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

from copy import deepcopy
from typing import List

import numpy as np
from ote_sdk.entities.annotation import AnnotationSceneEntity, AnnotationSceneKind, Annotation
from ote_sdk.entities.dataset_item import DatasetItemEntity
from ote_sdk.entities.datasets import DatasetEntity
from ote_sdk.entities.label import Domain, LabelEntity
from ote_sdk.utils.shape_factory import ShapeFactory

from mmdet.datasets.builder import DATASETS
from mmdet.datasets.custom import CustomDataset
from mmdet.datasets.pipelines import Compose


def get_annotation_mmdet_format(dataset_item: DatasetItemEntity, labels: List[LabelEntity]) -> dict:
    """
    Function to convert a OTE annotation to mmdetection format. This is used both in the OTEDataset class defined in
    this file as in the custom pipeline element 'LoadAnnotationFromOTEDataset'

    :param dataset_item: DatasetItem for which to get annotations
    :param labels: List of labels that are used in the task
    :return dict: annotation information dict in mmdet format
    """
    width, height = dataset_item.width, dataset_item.height

    # load annotations for item
    gt_bboxes = []
    gt_labels = []

    label_idx = {label.id: i for i, label in enumerate(labels)}

    for annotation in dataset_item.get_annotations(labels=labels, include_empty=False):

        box = ShapeFactory.shape_as_rectangle(annotation.shape)

        class_indices = [
            label_idx[label.id]
            for label in annotation.get_labels(include_empty=False)
            if label.domain == Domain.DETECTION
        ]

        n = len(class_indices)
        gt_bboxes.extend([[box.x1 * width, box.y1 * height, box.x2 * width, box.y2 * height] for _ in range(n)])
        gt_labels.extend(class_indices)

    if len(gt_bboxes) > 0:
        ann_info = dict(
            bboxes=np.array(gt_bboxes, dtype=np.float32).reshape(-1, 4),
            labels=np.array(gt_labels, dtype=int),
        )
    else:
        ann_info = dict(
            bboxes=np.zeros((0, 4), dtype=np.float32),
            labels=np.array([], dtype=int),
        )
    return ann_info


@DATASETS.register_module()
class OTEDataset(CustomDataset):
    """
    Wrapper that allows using a OTE dataset to train mmdetection models. This wrapper is not based on the filesystem,
    but instead loads the items here directly from the OTE DatasetEntity object.

    The wrapper overwrites some methods of the CustomDataset class: prepare_train_img, prepare_test_img and prepipeline
    Naming of certain attributes might seem a bit peculiar but this is due to the conventions set in CustomDataset. For
    instance, CustomDatasets expects the dataset items to be stored in the attribute data_infos, which is why it is
    named like that and not dataset_items.

    """

    class _DataInfoProxy:
        """
        This class is intended to be a wrapper to use it in CustomDataset-derived class as `self.data_infos`.
        Instead of using list `data_infos` as in CustomDataset, our implementation of dataset OTEDataset
        uses this proxy class with overriden __len__ and __getitem__; this proxy class
        forwards data access operations to ote_dataset and converts the dataset items to the view
        convenient for mmdetection.
        """
        def __init__(self, ote_dataset, labels):
            self.ote_dataset = ote_dataset
            self.labels = labels

        def __len__(self):
            return len(self.ote_dataset)

        def __getitem__(self, index):
            """
            Prepare a dict 'data_info' that is expected by the mmdet pipeline to handle images and annotations
            :return data_info: dictionary that contains the image and image metadata, as well as the labels of the objects
                in the image
            """

            dataset = self.ote_dataset
            item = dataset[index]

            height, width = item.height, item.width

            data_info = dict(dataset_item=item, width=width, height=height, index=index,
                             ann_info=dict(label_list=self.labels))

            return data_info

    def __init__(self, ote_dataset: DatasetEntity, labels: List[LabelEntity], pipeline, test_mode: bool = False, min_size=None):
        self.ote_dataset = ote_dataset
        self.labels = labels
        self.CLASSES = list(label.name for label in labels)
        self.test_mode = test_mode
        self.min_size = min_size

        # Instead of using list data_infos as in CustomDataset, this implementation of dataset
        # uses a proxy class with overriden __len__ and __getitem__; this proxy class
        # forwards data access operations to ote_dataset.
        # Note that list `data_infos` cannot be used here, since OTE dataset class does not have interface to
        # get only annotation of a data item, so we would load the whole data item (including image)
        # even if we need only checking aspect ratio of the image; due to it
        # this implementation of dataset does not uses such tricks as skipping images with wrong aspect ratios or
        # small image size, since otherwise reading the whole dataset during initialization will be required.
        self.data_infos = OTEDataset._DataInfoProxy(ote_dataset, labels)

        self.proposals = None  # Attribute expected by mmdet but not used for OTE datasets

        if not test_mode:
            self._set_group_flag()

        self.pipeline = Compose(pipeline)

    def _set_group_flag(self):
        """Set flag for grouping images.

        Originally, in Custom dataset, images with aspect ratio greater than 1 will be set as group 1,
        otherwise group 0.
        This implementation will set group 0 for every image.
        """
        self.flag = np.zeros(len(self), dtype=np.uint8)

    def _rand_another(self, idx):
        return np.random.choice(len(self))

    # In contrast with CustomDataset this implementation of dataset
    # does not filter images w.r.t. the min size
    def _filter_imgs(self, min_size=32):
        raise NotImplementedError

    def prepare_train_img(self, idx: int) -> dict:
        """Get training data and annotations after pipeline.

        :param idx: int, Index of data.
        :return dict: Training data and annotation after pipeline with new keys introduced by pipeline.
        """
        item = deepcopy(self.data_infos[idx])
        if self.min_size:
            item = self.filter_small_gt(item)
        self.pre_pipeline(item)
        return self.pipeline(item)

    def prepare_test_img(self, idx: int) -> dict:
        """Get testing data after pipeline.

        :param idx: int, Index of data.
        :return dict: Testing data after pipeline with new keys introduced by pipeline.
        """
        # FIXME.
        # item = deepcopy(self.data_infos[idx])
        item = self.data_infos[idx]
        self.pre_pipeline(item)
        return self.pipeline(item)

    @staticmethod
    def pre_pipeline(results: dict):
        """Prepare results dict for pipeline. Add expected keys to the dict. """
        results['bbox_fields'] = []
        results['mask_fields'] = []
        results['seg_fields'] = []

    def get_ann_info(self, idx):
        """
        This method is used for evaluation of predictions. The CustomDataset class implements a method
        CustomDataset.evaluate, which uses the class method get_ann_info to retrieve annotations.

        :param idx: index of the dataset item for which to get the annotations
        :return ann_info: dict that contains the coordinates of the bboxes and their corresponding labels
        """
        dataset_item = self.ote_dataset[idx]
        labels = self.labels
        return get_annotation_mmdet_format(dataset_item, labels)

    def filter_small_gt(self, item: dict) -> dict:
        """
        Function to filter instances in DatasetItem if its width or height is smaller than self.min_size

        :param item: 'data_info' dict that represents the DatasetItem
        :return dict: the same dict with filtered instances
        """
        dataset_item = item['dataset_item']
        width, height = dataset_item.width, dataset_item.height
        filtered_anns = []

        for ann in dataset_item.get_annotations():
            box = ann.shape
            if min(box.width * width, box.height * height) >= self.min_size:
                filtered_anns.append(ann)
        if len(filtered_anns) == 0:
            print(f'All instances on the image are smaller than min_size={self.min_size} - the image was skipped')

        dataset_item.annotation_scene.annotations = filtered_anns
        item['dataset_item'] = dataset_item
        return item
