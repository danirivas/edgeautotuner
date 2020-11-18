#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import base64
import json
import sys
import tempfile
import time

from flask import Flask, Response
from flask_restful import Resource, Api, reqparse
import cv2
import numpy as np
import pandas as pd
import torch
from torchvision.models import mobilenet_v2, resnet152
from torchvision.models.detection import faster_rcnn
from werkzeug.datastructures import FileStorage

sys.path.append('../')
from utils.detector import init_detector, run_detector
from utils.detector import ALL_MODELS as TF_MODELS

app = Flask(__name__)
api = Api(app)

videos = {}
video_results = {}
models = {}
framework = 'torch'


def infer_tf(model, img, device='cpu'):
    ts0 = time.time()
    results = run_detector(model, img, model.input_size)
    ts1 = time.time()
    print(f'inference took {ts1-ts0:.2f} seconds.')
    return results


@torch.no_grad()
def infer_torch(model, img, device='cpu'):
    dev = torch.device('cpu') if device == 'cpu' else device
    model.to(dev)

    # img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = np.array(img)
    img = img.astype("single") / float(255)
    x = torch.from_numpy(img).permute(2, 0, 1).unsqueeze(0)
    x = x.to(dev)

    ts0 = time.time()
    predictions = model(x)
    del x
    ts1 = time.time()
    print(f'inference took {ts1-ts0:.2f} seconds.')

    return predictions[0].detach().cpu().numpy()


def get_top_torch(preds, topn=10):
    if topn > len(preds):
        topn = len(preds)
    idxs = np.argpartition(-preds, 5)[:topn]
    results = []
    for i, idx in enumerate(idxs):
        results.append([str(idx), f'{preds[idx]:.2f}'])

    top = {
        'idxs': results[0],
        'scores': results[1]
    }
    return top


def get_top_tf(preds, topn=10):
    boxes = preds['detection_boxes'][0]
    scores = preds['detection_scores'][0]
    class_ids = preds['detection_classes'][0]

    top = {
        'boxes': boxes[:topn].tolist(),
        'scores': scores[:topn].tolist(),
        'idxs': class_ids[:topn].astype(int).tolist()
    }
    return top


def process_video(filename, model, device, framework):
    cap = cv2.VideoCapture(filename)
    ret, frame = cap.read()
    frame_id = 0
    m = models[model]

    data = []
    while ret:
        if framework == 'torch':
            device = device if device == 'cpu' else devices[model]
            preds = infer_torch(m, frame, device)
            data.append(get_top_torch(preds))

        else:
            preds = infer_tf(m, frame, device)
            data.append(get_top_tf(preds))

        frame_id += 1

        ret, frame = cap.read()

    return data

class Models(Resource):
    def get(self):
        tf_models = [['tf', m] for m in TF_MODELS.keys()]
        data = pd.DataFrame(tf_models, columns=['framework', 'model'])
        data = data.to_dict()
        return {'data': data}, 200

    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument('model', required=True)
        args = parser.parse_args()

        if args.model not in [m for m in TF_MODELS.keys()]:
            return {
                'message': 'Invalid model.'
            }, 401

        if args.model not in [m for m in models.keys()]:
            models[args.model] = init_detector(args.model)
        return {
            'message': 'Model loaded successfully.'
        }, 200


class Video(Resource):
    def get(self):
        parser = reqparse.RequestParser()
        parser.add_argument('video', required=True)
        parser.add_argument('model', required=True)
        args = parser.parse_args()

        if args.video not in videos.keys():
            return {'data': ['video not loaded', []]}, 200

        if args.model not in video_results[args.video].keys():
            return {'data': ['model not requested', []]}, 200

        status, data = video_results[args.video][args.model]
        return {'data': [status, data]}, 200

    def put(self):
        parser = reqparse.RequestParser()
        parser.add_argument('video', required=True)
        parser.add_argument('overwrite', default=False, type=bool, required=False)
        parser.add_argument('file', type=FileStorage, location='files')
        args = parser.parse_args()

        if not args.overwrite and args.video in videos.keys():
            print(f'{args.video} already in server')
            return {}, 204

        tf = tempfile.NamedTemporaryFile(delete=False)
        args.file.save(tf)
        videos[args.video] = tf
        video_results[args.video] = {}

        print(f'stored {args.video} in {tf.name}')
        return {}, 204

    def post(self):
        parser = reqparse.RequestParser()

        parser.add_argument('model', required=True)
        parser.add_argument('device', required=True)
        parser.add_argument('framework', required=True)
        parser.add_argument('video', required=True)
        # parser.add_argument('video', type=FileStorage, location='files')

        args = parser.parse_args()

        if args.model not in ['edge', 'ref']:
            return {
                'message': 'Invalid model.'
            }, 401

        if args.device not in ['cpu', 'cuda']:
            return {
                'message': 'Invalid device.'
            }, 401

        if args.framework not in ['torch', 'tf']:
            return {
                'message': 'Invalid framework.'
            }, 401

        if args.video not in videos.keys():
            return {
                'message': 'Video does not exist.'
            }, 401

        if args.model in video_results[args.video].keys():
            status, data = video_results[args.video][args.model]
            if status == 'ready':
                return Response(
                    response=json.dumps({
                        "data": data
                    }),
                    status=200,
                    mimetype='application/json'
                )

            else:
                print(f'Accepted a new request for {args.video} and model {args.model}')

        # tf = tempfile.NamedTemporaryFile(delete=True)
        # args.video.save(tf)
        tf = videos[args.video]

        video_results[args.video][args.model] = ['running', []]
        data = process_video(tf.name, args.model,
                      args.device, args.framework)

        video_results[args.video][args.model] = ['ready', data]

        return Response(
            response=json.dumps({
                "data": data
            }),
            status=200,
            mimetype='application/json'
        )


class Infer(Resource):
    def get(self):
        data = pd.DataFrame([], columns=['test'])
        data = data.to_dict()
        return {'data': data}, 200


    def post(self):
        print('received post request')
        parser = reqparse.RequestParser()

        parser.add_argument('img', required=True)
        parser.add_argument('model', required=True)
        parser.add_argument('device', required=True)
        parser.add_argument('framework', required=True)

        args = parser.parse_args()

        if args.model not in ['edge', 'ref']:
            return {
                'message': 'Invalid model.'
            }, 401
        
        if args.device not in ['cpu', 'cuda']:
            return {
                'message': 'Invalid device.'
            }, 401

        if args.framework not in ['torch', 'tf']:
            return {
                'message': 'Invalid framework.'
            }, 401


        png_img = base64.b64decode(args.img)
        nparr = np.frombuffer(png_img, np.uint8)
        img = cv2.imdecode(nparr, flags=1)

        m = models[args.model]
        
        if args.framework == 'torch':
            device = args.device if args.device == 'cpu' else devices[args.model]
            preds = infer_torch(m, img, device)
            return Response(
                response=json.dumps({
                    "data": get_top_torch(preds)
                }),
                status=200,
                mimetype='application/json'
            )
        else:
            preds = infer_tf(m, img, args.device)
            return Response(
                response=json.dumps({
                    "data": get_top_tf(preds)
                }),
                status=200,
                mimetype='application/json'
            )


api.add_resource(Infer, '/infer')
api.add_resource(Models, '/models')
api.add_resource(Video, '/video')


def main():
    global models
    global devices
    global app
    global api
    global framework

    args = argparse.ArgumentParser()
    args.add_argument("-f", "--framework", 
                      default='torch',
                      choices=['torch', 'tf'],
                      help="Framework to use")

    args.add_argument("-p", "--port",
                      default=5000,
                      type=int,
                      help="Port to listen to.")

    config = args.parse_args()
    framework = config.framework
    
    if config.framework == 'torch':
        models['edge'] = mobilenet_v2(pretrained=True)
        models['ref'] = resnet152(pretrained=True)

        devices = {}
        if torch.cuda.is_available():
            devices['edge'] = torch.device('cuda:0')
            devices['ref'] = torch.device('cuda:1')
        else:
            devices['edge'] = 'cpu'
            devices['ref'] = 'cpu'

        models['edge'].eval()
        models['ref'].eval()
    elif config.framework == 'tf':
        ref_model = 'Faster R-CNN Inception ResNet V2 1024x1024'
        models['edge'] = init_detector()
        models['ref'] = init_detector(ref_model)
        models['edge'].input_size = (320, 320)
        models['ref'].input_size = (1024, 1024)

    app.run(port=config.port)
    

if __name__ == '__main__':
    main()
