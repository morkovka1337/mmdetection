# Copyright (C) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
from addict import Dict as ADDict
from compression.api import DataLoader
from compression.engines.ie_engine import IEEngine
from compression.graph import load_model, save_model
from compression.graph.model_utils import compress_model_weights, get_nodes_by_type
from compression.pipeline.initializer import create_pipeline
from ote_sdk.entities.annotation import Annotation, AnnotationSceneEntity, AnnotationSceneKind
from ote_sdk.entities.datasets import DatasetEntity
from ote_sdk.entities.inference_parameters import InferenceParameters, default_progress_callback
from ote_sdk.entities.label import LabelEntity
from ote_sdk.entities.model import (
    ModelEntity,
    ModelFormat,
    ModelOptimizationType,
    ModelPrecision,
    ModelStatus,
    OptimizationMethod,
)
from ote_sdk.entities.optimization_parameters import OptimizationParameters
from ote_sdk.entities.resultset import ResultSetEntity
from ote_sdk.entities.scored_label import ScoredLabel
from ote_sdk.entities.shapes.rectangle import Rectangle
from ote_sdk.entities.task_environment import TaskEnvironment
from ote_sdk.usecases.evaluation.metrics_helper import MetricsHelper
from ote_sdk.usecases.exportable_code.inference import BaseOpenVINOInferencer
from ote_sdk.usecases.tasks.interfaces.evaluate_interface import IEvaluationTask
from ote_sdk.usecases.tasks.interfaces.inference_interface import IInferenceTask
from ote_sdk.usecases.tasks.interfaces.optimization_interface import IOptimizationTask, OptimizationType
from ote_sdk.serialization.label_mapper import label_schema_to_bytes

from .configuration import OTEDetectionConfig
from mmdet.utils.logger import get_root_logger

logger = get_root_logger()


def get_output(net, outputs, name):
    try:
        key = net.get_ov_name_for_tensor(name)
        assert key in outputs, f'"{key}" is not a valid output identifier'
    except KeyError:
        if name not in outputs:
            raise KeyError(f'Failed to identify output "{name}"')
        key = name
    return outputs[key]


def extract_detections(output, net, input_size):
    if 'detection_out' in output:
        detection_out = output['detection_out']
        output['labels'] = detection_out[0, 0, :, 1].astype(np.int32)
        output['boxes'] = detection_out[0, 0, :, 3:] # * np.tile(input_size, 2)
        output['boxes'] = np.concatenate((output['boxes'], detection_out[0, 0, :, 2:3]), axis=1)
        del output['detection_out']
        return output

    outs = output
    output = {
        'labels': get_output(net, outs, 'labels'),
        'boxes': get_output(net, outs, 'boxes')
    }
    valid_detections_mask = output['labels'] >= 0
    output['labels'] = output['labels'][valid_detections_mask]
    output['boxes'] = output['boxes'][valid_detections_mask]
    output['boxes'][:, :4] /= np.tile(input_size, 2)[None]
    output['boxes'] = output['boxes'].astype(float)
    return output


class OpenVINODetectionInferencer(BaseOpenVINOInferencer):
    def __init__(
        self,
        labels: List[LabelEntity],
        model_file: Union[str, bytes],
        weight_file: Union[str, bytes, None] = None,
        confidence_threshold: float = 0.0,
        device: str = "CPU",
        num_requests: int = 1,
    ):
        """
        Inferencer implementation for OTEDetection using OpenVINO backend.

        :param labels: List of labels that was used during model training.
        :param model_file: Path OpenVINO IR model definition file.
        :param weight_file: Path OpenVINO IR model weights file.
        :param confidence_threshold: Confidence threshold for passing detection to the output.
        :param device: Device to run inference on, such as CPU, GPU or MYRIAD. Defaults to "CPU".
        :param num_requests: Maximum number of requests that the inferencer can make. Defaults to 1.

        """
        super().__init__(model_file, weight_file, device, num_requests)
        self.labels = labels
        self.input_blob_name = 'image'
        self.n, self.c, self.h, self.w = self.net.input_info[self.input_blob_name].tensor_desc.dims
        self.keep_aspect_ratio_resize = False
        self.pad_value = 0
        self.confidence_threshold = confidence_threshold

    @staticmethod
    def resize_image(image: np.ndarray, size: Tuple[int], keep_aspect_ratio: bool = False) -> np.ndarray:
        if not keep_aspect_ratio:
            resized_frame = cv2.resize(image, size)
        else:
            h, w = image.shape[:2]
            scale = min(size[1] / h, size[0] / w)
            resized_frame = cv2.resize(image, None, fx=scale, fy=scale)
        return resized_frame

    def pre_process(self, image: np.ndarray) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
        resized_image = self.resize_image(image, (self.w, self.h), self.keep_aspect_ratio_resize)
        meta = {'original_shape': image.shape,
                'resized_shape': resized_image.shape}

        h, w = resized_image.shape[:2]
        if h != self.h or w != self.w:
            resized_image = np.pad(resized_image, ((0, self.h - h), (0, self.w - w), (0, 0)),
                                   mode='constant', constant_values=self.pad_value)
        # resized_image = self.input_transform(resized_image)
        resized_image = resized_image.transpose((2, 0, 1))  # Change data layout from HWC to CHW
        resized_image = resized_image.reshape((self.n, self.c, self.h, self.w))
        dict_inputs = {self.input_blob_name: resized_image}
        return dict_inputs, meta

    def post_process(self, prediction: Dict[str, np.ndarray], metadata: Dict[str, Any]) -> AnnotationSceneEntity:
        detections = extract_detections(prediction, self.net, (self.w, self.h))
        scores = detections['boxes'][:, 4]
        boxes = detections['boxes'][:, :4]
        labels = detections['labels']

        resized_image_shape = metadata['resized_shape']
        scale_x = self.w / resized_image_shape[1]
        scale_y = self.h / resized_image_shape[0]
        boxes[:, :4] *= np.array([scale_x, scale_y, scale_x, scale_y], dtype=boxes.dtype)

        areas = np.maximum(boxes[:, 3] - boxes[:, 1], 0) * np.maximum(boxes[:, 2] - boxes[:, 0], 0)
        valid_boxes = (scores >= self.confidence_threshold) & (areas > 0)
        scores = scores[valid_boxes]
        boxes = boxes[valid_boxes]
        labels = labels[valid_boxes]
        boxes_num = len(boxes)

        annotations = []
        for i in range(boxes_num):
            if scores[i] < self.confidence_threshold:
                continue
            assigned_label = [ScoredLabel(self.labels[labels[i]], probability=scores[i])]
            annotations.append(Annotation(
                Rectangle(x1=boxes[i, 0], y1=boxes[i, 1], x2=boxes[i, 2], y2=boxes[i, 3]),
                labels=assigned_label))

        return AnnotationSceneEntity(
            kind=AnnotationSceneKind.PREDICTION,
            annotations=annotations)

    def forward(self, inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        return self.model.infer(inputs)


class OTEOpenVinoDataLoader(DataLoader):
    def __init__(self, dataset: DatasetEntity, inferencer: BaseOpenVINOInferencer):
        self.dataset = dataset
        self.inferencer = inferencer

    def __getitem__(self, index):
        image = self.dataset[index].numpy
        annotation = self.dataset[index].annotation_scene
        inputs, metadata = self.inferencer.pre_process(image)

        return (index, annotation), inputs, metadata

    def __len__(self):
        return len(self.dataset)

class OpenVINODetectionTask(IInferenceTask, IEvaluationTask, IOptimizationTask):
    def __init__(self, task_environment: TaskEnvironment):
        logger.info('Loading OpenVINO OTEDetectionTask')
        self.task_environment = task_environment
        self.model = self.task_environment.model
        self.confidence_threshold: float = 0.0
        self.inferencer = self.load_inferencer()
        logger.info('OpenVINO task initialization completed')

    @property
    def hparams(self):
        return self.task_environment.get_hyper_parameters(OTEDetectionConfig)

    def load_inferencer(self) -> OpenVINODetectionInferencer:
        labels = self.task_environment.label_schema.get_labels(include_empty=False)
        self.confidence_threshold = np.frombuffer(self.model.get_data("confidence_threshold"), dtype=np.float32)[0]
        return OpenVINODetectionInferencer(labels,
                                           self.model.get_data("openvino.xml"),
                                           self.model.get_data("openvino.bin"),
                                           self.confidence_threshold)

    def infer(self, dataset: DatasetEntity, inference_parameters: Optional[InferenceParameters] = None) -> DatasetEntity:
        logger.info('Start OpenVINO inference')
        update_progress_callback = default_progress_callback
        if inference_parameters is not None:
            update_progress_callback = inference_parameters.update_progress
        dataset_size = len(dataset)
        for i, dataset_item in enumerate(dataset, 1):
            predicted_scene = self.inferencer.predict(dataset_item.numpy)
            dataset_item.append_annotations(predicted_scene.annotations)
            update_progress_callback(int(i / dataset_size * 100))
        logger.info('OpenVINO inference completed')
        return dataset

    def evaluate(self,
                 output_result_set: ResultSetEntity,
                 evaluation_metric: Optional[str] = None):
        logger.info('Start OpenVINO metric evaluation')
        if evaluation_metric is not None:
            logger.warning(f'Requested to use {evaluation_metric} metric, but parameter is ignored. Use F-measure instead.')
        output_result_set.performance = MetricsHelper.compute_f_measure(output_result_set).get_performance()
        logger.info('OpenVINO metric evaluation completed')

    def optimize(self,
                 optimization_type: OptimizationType,
                 dataset: DatasetEntity,
                 output_model: ModelEntity,
                 optimization_parameters: Optional[OptimizationParameters]):
        logger.info('Start POT optimization')

        if optimization_type is not OptimizationType.POT:
            raise ValueError('POT is the only supported optimization type for OpenVino models')

        data_loader = OTEOpenVinoDataLoader(dataset, self.inferencer)

        with tempfile.TemporaryDirectory() as tempdir:
            xml_path = os.path.join(tempdir, "model.xml")
            bin_path = os.path.join(tempdir, "model.bin")
            with open(xml_path, "wb") as f:
                f.write(self.model.get_data("openvino.xml"))
            with open(bin_path, "wb") as f:
                f.write(self.model.get_data("openvino.bin"))

            model_config = ADDict({
                'model_name': 'openvino_model',
                'model': xml_path,
                'weights': bin_path
            })

            model = load_model(model_config)

            if get_nodes_by_type(model, ['FakeQuantize']):
                logger.warning("Model is already optimized by POT")
                output_model.model_status = ModelStatus.FAILED
                return

        engine_config = ADDict({
            'device': 'CPU'
        })

        stat_subset_size = self.hparams.pot_parameters.stat_subset_size
        preset = self.hparams.pot_parameters.preset.name.lower()

        algorithms = [
            {
                'name': 'DefaultQuantization',
                'params': {
                    'target_device': 'ANY',
                    'preset': preset,
                    'stat_subset_size': min(stat_subset_size, len(data_loader))
                }
            }
        ]

        engine = IEEngine(config=engine_config, data_loader=data_loader, metric=None)

        pipeline = create_pipeline(algorithms, engine)

        compressed_model = pipeline.run(model)

        compress_model_weights(compressed_model)

        with tempfile.TemporaryDirectory() as tempdir:
            save_model(compressed_model, tempdir, model_name="model")
            with open(os.path.join(tempdir, "model.xml"), "rb") as f:
                output_model.set_data("openvino.xml", f.read())
            with open(os.path.join(tempdir, "model.bin"), "rb") as f:
                output_model.set_data("openvino.bin", f.read())
            output_model.set_data("confidence_threshold", np.array([self.confidence_threshold], dtype=np.float32).tobytes())
            
        output_model.set_data("label_schema.json", label_schema_to_bytes(self.task_environment.label_schema))

        # set model attributes for quantized model
        output_model.model_status = ModelStatus.SUCCESS
        output_model.model_format = ModelFormat.OPENVINO
        output_model.optimization_type = ModelOptimizationType.POT
        output_model.optimization_methods = [OptimizationMethod.QUANTIZATION]
        output_model.precision = [ModelPrecision.INT8]

        self.model = output_model
        self.inferencer = self.load_inferencer()
        logger.info('POT optimization completed')
