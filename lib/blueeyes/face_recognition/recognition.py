import os
import cv2
import sys
import pdb
import enum
import pickle
import joblib
import traceback
import threading
import multiprocessing
import numpy as np
from time import time
from scipy.spatial import distance

from functools import partial

from tqdm.notebook import tqdm

from tensorflow.keras.models import Model, load_model

# KNN classifier
from sklearn.neighbors import KNeighborsClassifier

from sklearn.utils import shuffle
from skimage.transform import resize

import face_recognition

# def preprocess_image__(x):
#         # Resize the image to have the shape of (128, 128)
#         x = resize(x, (self.target_size, self.target_size),
#             mode='constant',
#             anti_aliasing=False)
#         x = x.astype(np.float32) / 255
#         x = np.expand_dims(x, axis=0)
#         return x
    
def preprocess_image(img, target_size, norm=False):
    try:
        black = np.zeros((target_size, target_size, 3), dtype='uint8')
        if img.shape[0] >= img.shape[1]:
            y_scale = target_size / img.shape[0]
            interpolation = cv2.INTER_CUBIC if y_scale > 1 else cv2.INTER_AREA
            img = cv2.resize(img, None, fx=y_scale, fy=y_scale, interpolation=interpolation)
            y1 = (target_size - img.shape[1]) // 2
            black[:, y1:y1+img.shape[1], :] = img[:,:,:]
        else:
            x_scale = target_size / img.shape[1]
            interpolation = cv2.INTER_CUBIC if x_scale > 1 else cv2.INTER_AREA
            img = cv2.resize(img, None, fx=x_scale, fy=x_scale, interpolation=interpolation)
            x1 = (target_size - img.shape[0]) // 2
            black[x1:x1+img.shape[0], :, :] = img[:,:,:]
    except:
        print(y1, img.shape)
    if norm:
        black = black.astype(np.float32) / 255
        black = np.expand_dims(black, axis=0)
    return black

def face_roi(frame, box):
    (x1, y1, x2, y2) = box
    return frame[y1:y2, x1:x2]

class TrainOption(enum.Enum):
    RETRAIN = 1
    UPDATE = 2
    RUNONLY = 3

class FeatureExtractor:
    def __init__(self, model_type='face_recognition', model_dir=None):
        self.model_dir = '/home/huy/face_recog/models/mobilenetv2_checkpoint_60-0.96.hdf5'
        self.input_shape = ()
        self.output_shape = ()
        self.model_type = model_type
        
        # load model
        if model_type == 'dlib':
            import dlib
            model_path = os.path.abspath(os.path.join(__file__, '../../../../models/feature_extraction/dlib_face_recognition_resnet_model_v1.dat'))
            shape_predictor_path = os.path.abspath(os.path.join(__file__, '../../../../models/feature_extraction/shape_predictor_5_face_landmarks.dat'))
            self.shape_predictor = dlib.shape_predictor(shape_predictor_path)
            self.model = dlib.face_recognition_model_v1(model_path)
        elif model_type == 'vggface':
            import keras_vggface
            from keras_vggface.utils import preprocess_input

            self.model = keras_vggface.VGGFace(model='resnet50', include_top=False, input_shape=(224,224,3), pooling='avg')
            self.input_shape = (224, 224, 3)
            self.output_shape = (1,256)
        elif model_type == 'mobilenet':
            self.model = load_model(self.model_dir)
            self.model = Model(self.model.inputs, self.model.layers[-3].output)
            self.input_shape = self.model.input_shape[1:]
            self.output_shape = self.model.output_shape[1:]
            self.model.use_learning_phase = False
        elif model_type == 'face_recognition':
            import face_recognition
        elif model_type == 'openface':
            # Open Face feature extractor
            sys.path.append('../../OpenFacePytorch')
            import torch
            from OpenFacePytorch import OpenFace
            self.model = OpenFace.prepareOpenFace()
            self.model = self.model.eval()
        else:
            print('Feature Extractor type not found!')
            raise NameError
            
    def feed(self, input_data):
#         if input_data != input_shape:
#             print('Input shape not match!')
#             raise ValueError
        features = []
        input_data = [preprocess_image(img, 150) for img in input_data]
        if self.model_type == 'dlib': 
            import dlib
            landmarks = []
            for img in input_data:
                box = dlib.rectangle(0, 0, img.shape[0], img.shape[1])
                landmark = self.shape_predictor(img, box)
                objs = dlib.full_object_detections()
                objs.append(landmark)
                landmarks.append(objs)
            features = self.model.compute_face_descriptor(input_data, landmarks, num_jitters=1)
            features = [f[0] for f in features]
        elif self.model_type == 'face_recognition':
            for img in input_data:
                known_face_box = [(0, img.shape[1], img.shape[0], 0)]
                features.append(face_recognition.face_encodings(img, known_face_locations=known_face_box)[0])
        elif self.model_type == 'mobilenet':
            input_data = cv2.resize(input_data, self.input_shape[0:2])
            input_data = input_data.astype(np.float32) / 255
            input_feed = np.expand_dims(input_data, axis=0)
            features = self.model.predict(input_feed)
        elif self.model_type == 'vggface':
            input_data = cv2.resize(input_data, self.input_shape[0:2])
            features = _vgg_encoding(input_data)
            input_data = OpenFace.process_img(input_data)
#             input_data_ = torch.cat(input_data, 0)
            input_data_ = input_data
            input_data_ = torch.autograd.Variable(input_data_, requires_grad=False)
            f, f_736 = self.model(input_data_)
            features = f.cpu().detach().numpy()[0]
        return features

    def _vgg_encoding(self, image):
        sample = sample.astype('float32')
        sample = np.expand_dims(sample, axis=0)
        sample = preprocess_input(sample, version=2)
        yhat = self.vgg_model.predict(sample)
        return yhat

class FaceRecognition:
    FRAME_COUNT_TO_DECIDE = 10
    def __init__(self, model_dir='', model_path='', feature_extractor_type='face_recognition', knn_opts=(7, 'euclidean', 'distance') , classifier_method='distance', trainopt=TrainOption.RUNONLY):
        self.target_size = 128
        self.model_dir = model_dir
        self.result_buffer = []
        self.feature_extractor_type = feature_extractor_type
        self.classifier_method = classifier_method
        self.feature_extractor = FeatureExtractor(model_type=feature_extractor_type)
            
        # if not os.path.exists(os.path.join(model_dir, 'model.dat')) or trainopt==TrainOption.RETRAIN:
        #     self.model = []
        #     self.labels = []
        #     self._create_model()
        # elif trainopt == TrainOption.UPDATE:
        #     self._create_model(TrainOption.UPDATE)
        # elif trainopt == TrainOption.RUNONLY:
        if self.classifier_method == 'knn':
            self._load_model(model_dir=model_dir)
        elif self.classifier_method == 'nn':
            self._load_model()
        elif self.classifier_method == 'kmeans':
            pass
        elif self.classifier_method == 'euclid':
            self._load_model(model_dir=model_dir)
        elif self.classifier_method == 'svm':
            self._load_model(model_path=model_path)
        else:
            print('Classifier not found!')
            raise NameError
    def config_postprocessing(self, **kwargs):
        FRAME_COUNT_TO_DECIDE = kwargs['FRAME_COUNT_TO_DECIDE']

    
        # features, labels = cls.create_train_set(train_set_dict, output_model_location=output_model_location)
        # classes_file = open(os.path.join(output_model_location, 'classes.dat'), 'w')
        # model_file = open(os.path.join(output_model_location, 'model.dat'), 'wb')

        # classes = list(np.unique(labels))
        # classes_file.write('\n'.join(classes))
        # classes_file.close()

        # for class_name in classes:
        #     for 
        #     mean_vec = np.mean([features[label.index(i)] for label in labels if label == class_name], axis=0)
        #     print(mean_vec)
        #     model.append(mean_vec)

        # np.save(model_file, model)
        # model_file.close()
        # return model, classes


    # preprocess image before feeding to the network

    def _load_model(self, **kwargs):
        if self.classifier_method == 'knn':
            self.knn = pickle.load(open(self.model_dir + '/knn_clf.pkl', 'rb'))
        elif self.classifier_method == 'nn':
            MODEL_DIR = '/home/huy/face_recog/models/nn/'
            self.model = load_model(MODEL_DIR + 'mobilenetv2_checkpoint_60-0.96.hdf5')
            self.classes = np.load(MODEL_DIR + 'classes.npy')
        elif self.classifier_method == 'euclid':
            with open(os.path.join(self.model_dir, 'model.dat'), 'rb') as model_file:
                self.model = np.load(model_file, allow_pickle=True)
            with open(os.path.join(self.model_dir, 'classes.dat'), 'rb') as classes_file:
                self.classes = np.load(classes_file, allow_pickle=True)
        elif self.classifier_method == 'svm':
            self.svm_clf = joblib.load(kwargs['model_path'])

    def extract_feature(self, frames):
        features = self.feature_extractor.feed(frames)
        return features

    def _knn_recog(self, features, **kwargs):
        result = []
        try:
            probas_list = self.knn.predict_proba(features)
            for probas in probas_list:
                if np.max(probas) >= kwargs['threshold']:
                    label = self.knn.classes_[np.argmax(probas)]
                    result.append(label)  
                else:
                    result.append('unknown')
        except:
            traceback.print_exc()
        return result
    
    def _svm_recog(self, features, **kwargs):
        try:
            result = []
            
            probas_list = self.svm_clf.predict_proba(features)
            for probas in probas_list:
                if np.max(probas) >= kwargs['threshold']:
                    label = self.svm_clf.classes_[np.argmax(probas)]
                    result.append(label)  
                else:
                    result.append('unknown')
        except:
            traceback.print_exc()
        return result
            
#     def _vgg_recog(self, frame, boxes, recog_level=1, threshold=0.5):
#         result = []
#         for (x1, y1, x2, y2) in boxes:
#             # print(x1,y1,x2,y2)
#             crop = frame[y1:y2, x1:x2, :]
#             cv2.imshow('debug', crop)
#             cv2.waitKey(0)
#             target_face = self._vgg_encoding(crop)[0]
#             min_distance = 1000
#             predict_label = ['unknown']
#             for model_face_list, label in zip(self.model, self.labels):
#                 dis = distance.euclidean(target_face, model_face_list[0])
#                 if dis < min_distance:
#                     min_distance = dis
#                     predict_label = [label]
#             if min_distance > threshold:
#                 result.append(['unknown'])
#             else:
#                 result.append(predict_label)
#         return result

    def _distance_recog(self, features, recog_level=1, threshold=0.5):
        try:
            result = []
            for feature in features:
                predict_label = 'unknown'
                prepare_list = np.repeat([[feature]], len(self.model), axis=0)
                result_list = np.linalg.norm(prepare_list-self.model, axis=2)
                min_distance = np.min(result_list)
                # print(min_distance, np.argmin(result_list))
                # print(prepare_list.shape, self.model.shape, result_list.shape)
                predict_label = [self.classes[np.argmin(result_list)]]
                
                if min_distance > threshold:
                    result.append('unknown')
                else:
                    result.append(predict_label)
        except:
            # print(result_list)
            traceback.print_exc()
            print('target_face.shape', features.shape)
            print('np.argmin(result_list)', np.argmin(result_list))
        return result
        # match_count = 0
        # total_count = 0
        #     for model_face_list, label in zip(self.model, self.labels):
        #         if match_count > 0:
        #             break
        #         else:
        #             match_count = 0
        #             total_count = 0
        #         for i in range(recog_level):
        #             try:
        #                 match = face_recognition.compare_faces([model_face_list[i]], target_face, tolerance=threshold)
        #             except Exception as e:
        #                 print(e)
        #                 print(model_face_list[i])
        #                 print(target_face)
        #             else:
        #                 total_count += 1
        #                 if match == True:
        #                     match_count += 1
        #         if total_count != 0:
        #             if match_count/total_count > 0.5:
        #                 result.append([label])
        #     if not match_count:
        #         result.append(['unknown'])
        # return result

    def _nn_recog(self, feature, **kwargs):
        try:
            probas = self.model.predict([feature])
            if np.max(probas) >= kwargs['threshold']:
                label = self.classes[np.argmax(probas)]
                result = label
            else:
                result = 'unknown'
        except:
            traceback.print_exc()
        return result

    def recog(self, features, **kwargs):
        if self.classifier_method == 'knn':
            result = self._knn_recog(features, **kwargs)
        elif self.classifier_method == 'nn':
            result = self._nn_recog(features, **kwargs)
        elif self.classifier_method == 'svm':
            result = self._svm_recog(features, **kwargs)
        else:
            result = self._distance_recog(features, **kwargs)
        result = [r.split('_')[0] for r in result]
        return result 

    def put_to_result_buffer(self, boxes, labels):
        num_person = len(boxes)
        # Add raw data to buffer
        if len(self.result_buffer) >= self.FRAME_COUNT_TO_DECIDE:
            self.result_buffer.pop(0)
        if num_person == 0:
            # result_buffer.append([])
            pass
        else: # more than 1 person
            self.result_buffer.append(labels)
        self._event.set()

    def postprocessing(self, event, callback):
        try:
            while True:
                # if num_person == 2:
                #     print(labels)
                #     while True:
                #         pass
                event.wait()
                num_result = 0
                # Check result buffer to decide what to print
                id_count = {}
                # Wait for the buffer to fill up and loop through buffer
                if len(self.result_buffer) >= self.FRAME_COUNT_TO_DECIDE:
                    deep = max([len(lst) for lst in self.result_buffer])
                    for row in range(deep):
                        for col in range(self.FRAME_COUNT_TO_DECIDE):
                            try:
                                ID = self.result_buffer[col][row][0]
                                if ID in id_count.keys():
                                    id_count[ID] += 1
                                else:
                                    id_count[ID] = 1
                            except IndexError:
                                break
                            else:
                                num_result += 1

                num_id = len(id_count.keys())
                if num_id < num_result:
                    num_result = num_id
                # print(result_buffer)
                # print(num_result)
                # print(id_count)
                result_id = []
                if id_count:
                    frequency_ids = [(k,v) for k, v in sorted(id_count.items(), key=lambda item: item[1])]
                    for i in range(num_result):
                        if frequency_ids[i][1] > int(0.6*self.FRAME_COUNT_TO_DECIDE):
                            result_id.append(frequency_ids[i][0].replace('\n', ''))
                if len(result_id) > 0:
                    callback(result_id)
                event.clear()
        except:
            traceback.print_exc()

    def on_final_decision(self, callback):
        self._event = threading.Event()
        self._thread = threading.Thread(target=self.postprocessing, args=(self._event, callback))
        self._thread.start()

class ModelTraining:
    def __init__(self, feature_extractor):
        self.feature_extractor = feature_extractor
    
    def split_into(self, d, n_part):
        parts = []
        d = list(d.items())
        part_size = len(d)//n_part
        i = 0
        while i < n_part:
            parts.append(dict(d[i*part_size:(i*part_size+part_size)]))
            i += 1
        for i in range(len(d) % n_part):
            key, value = d[n_part*part_size+i]
            parts[i][key] = value
        return parts

    def process_batch(self, d):
        count = 0
        features = []
        ids = []
        for id, img_paths in d.items():
            # pdb.set_trace()
            count += 1
            ids.extend([id]*len(img_paths))
            print(f'{count}/{len(d.keys())}')
            with tqdm(total=len(img_paths)) as pbar:
                for i in range(0, len(img_paths), 64):
                    try:
                        imgs = []
                        if i == (len(img_paths)//64)*64:
                            n = len(img_paths)
                        else:
                            n = i+64
                        for k in range(i,n):
                            img = cv2.imread(img_paths[k])
                            img = preprocess_image(img, 150)
                            imgs.append(img)
                        vectors = self.feature_extractor.feed(imgs)
                        vectors = np.array(vectors)
                        features.extend(vectors)
                        pbar.update(n-i)
                    except:
                        traceback.print_exc()
        return list(zip(features,ids))
    
    def create_train_set(self, train_set_dict, detect_face=False, output_model_location='.', process=6):        
        # create model dir is the dir is not exist
        if not os.path.exists(output_model_location):
            os.makedirs(output_model_location)
        features = []
        # with multiprocessing.Pool(process) as pool:
        #     return_value = pool.map(self.process_batch, self.split_into(train_set_dict, process))
        #     features.extend(return_value)
        # output = []
        # for batch in features:
        #     output.extend(batch)
        output = self.process_batch(train_set_dict)
        joblib.dump(output, os.path.join(output_model_location, 'features.joblib'))
        return output

    def train_knn(self, features, labels, K=7, metric='euclidean', weights='distance', output_model_location='.'):
        knn = KNeighborsClassifier(n_neighbors=K, metric=metric, weights=weights)
        model_pkl = open(os.path.join(output_model_location, 'knn_clf.pkl'), 'wb')
        knn.fit(features, labels)
        print(knn)
        pickle.dump(knn, model_pkl)

    def train_simple_model(self, features, labels, detect_face=False, output_model_location='.'):
        model_file = open(os.path.join(output_model_location, 'model.dat'), 'wb')
        classes_file = open(os.path.join(output_model_location, 'classes.dat'), 'wb')
        model = {}
        for feature, label in zip(features, labels):
            if label not in model:
                model[label] = feature
            model[label] = (model[label] + feature)/2
            
        classes, centroid_vec = tuple(zip(*model.items()))
        np.save(model_file, centroid_vec)
        np.save(classes_file, classes)
        model_file.close()
        classes_file.close()
        return model
            
    
if __name__ == '__main__':
    recog = Recognition()
