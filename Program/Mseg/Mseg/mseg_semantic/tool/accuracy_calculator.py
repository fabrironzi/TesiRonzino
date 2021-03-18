#!/usr/bin/python3

import cv2
import imageio
import logging
import numpy as np
import os
from pathlib import Path
import pdb
import torch
from typing import List, Tuple

from mseg.utils.cv2_utils import cv2_imread_rgb
from mseg.utils.dir_utils import check_mkdir
from mseg.utils.mask_utils import save_pred_vs_label_7tuple,save_pred_vs_label_4tuple
from mseg.utils.names_utils import load_class_names, get_dataloader_id_to_classname_map
from mseg.taxonomy.taxonomy_converter import TaxonomyConverter

from mseg_semantic.utils.avg_meter import AverageMeter, SegmentationAverageMeter
from mseg_semantic.utils.confusion_matrix_renderer import ConfusionMatrixRenderer


"""
Given a set of inference results (inferred label maps saved as grayscale images),
compute the accuracy vs. ground truth label maps.

Expects inference results to be saved as {save_folder}/gray/*.png, exactly as our
test scripts spit out.
"""


def get_logger():
    """
    """
    logger_name = "main-logger"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        fmt = "[%(asctime)s %(levelname)s %(filename)s line %(lineno)d %(process)d] %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
    return logger

logger = get_logger()

def get_unique_stem_from_last_k_strs(fpath: str, k: int = 4) -> str:
    """
    For datasets like ScanNet where image filename stem is not unique.
        Args:
        -   fpath
        -   k

        Returns:
        -   unique_stem: string
    """
    parts = Path(fpath).parts
    concat_kparent_dirs = '_'.join(parts[-k:-1])
    unique_stem = concat_kparent_dirs + '_' + Path(fpath).stem
    return unique_stem


class AccuracyCalculator:
    def __init__(
        self,
        args,
        data_list: List[Tuple[str,str]],
        dataset_name: str,
        class_names: List[str],
        save_folder: str,
        num_eval_classes: int,
        render_confusion_matrix: bool = False
    ) -> None:
        """
            Args:
            -   args,
            -   data_list
            -   dataset_name: 
            -   class_names: 
            -   save_folder: 
            -   num_eval_classes: 
            -   render_confusion_matrix: 

            Returns:
            -   None
        """
        self.num_eval_classes = num_eval_classes
        self.args = args
        self.data_list = data_list
        self.dataset_name = dataset_name
        self.class_names = class_names
        self.save_folder = save_folder
        self.gray_folder = os.path.join(save_folder, 'gray')
        self.render_confusion_matrix = render_confusion_matrix

        if self.render_confusion_matrix:
            self.cmr = ConfusionMatrixRenderer(self.save_folder, class_names, self.dataset_name)
        self.sam = SegmentationAverageMeter()
        self.id_to_class_name_map = get_dataloader_id_to_classname_map(
            self.dataset_name,
            class_names,
            include_ignore_idx_cls=True
        )
        self.tc = TaxonomyConverter()
        self.excluded_ids = []

        assert isinstance(args.vis_freq, int)
        assert isinstance(args.img_name_unique, bool)
        assert isinstance(args.taxonomy, str)
        assert isinstance(args.model_path, str)

    def execute(self, save_vis: bool = True) -> None:
        """
            Args:
            -   save_vis: whether to save visualize examplars
        """
        self.evaluate_predictions(save_vis)
        self.print_results()
        self.dump_acc_results_to_file()


    def convert_label_to_pred_taxonomy(self, target_img):
        """ """
        if self.args.taxonomy == 'universal':
            _, target_img = ToFlatLabel(self.tc, self.args.dataset)(target_img, target_img)
            return target_img.type(torch.uint8).numpy()
        else:
            return target_img

    def evaluate_predictions(self, save_vis: bool = True) -> None:
        """ Calculate accuracy.

            Args:
            -   data_list: 
            -   pred_folder: 

            Returns:
            -   None
        """
        pred_folder = self.gray_folder
        for i, (image_path, target_path) in enumerate(self.data_list):
            if self.args.img_name_unique:
                image_name = Path(image_path).stem
            else:
                image_name = get_unique_stem_from_last_k_strs(image_path)

            pred = cv2.imread(os.path.join(pred_folder, image_name+'.png'), cv2.IMREAD_GRAYSCALE)

            target_img = imageio.imread(target_path)
            target_img = target_img.astype(np.int64)

            target_img = self.convert_label_to_pred_taxonomy(target_img)
            self.sam.update_metrics_cpu(pred, target_img, self.num_eval_classes)


            if (i+1) % self.args.vis_freq == 0:
                print_str = f'Evaluating {i + 1}/{len(self.data_list)} on image {image_name+".png"},' + \
                    f' accuracy {self.sam.accuracy:.4f}.'
                logger.info(print_str)

            if save_vis:
                if (i+1) % self.args.vis_freq == 0:
                    mask_save_dir = pred_folder.replace('gray', 'rgb_mask_predictions')
                    grid_save_fpath = f'{mask_save_dir}/{image_name}.png'
                    rgb_img = cv2_imread_rgb(image_path)
                    save_pred_vs_label_7tuple(rgb_img, pred, target_img, self.id_to_class_name_map, grid_save_fpath)


    def print_results(self):
        """
        Dump per-class IoUs and mIoU to stdout.
        """
        if self.args.taxonomy == 'universal' and (self.args.dataset in self.tc.train_datasets):
            iou_class, accuracy_class, mIoU, mAcc, allAcc = self.sam.get_metrics(
                exclude=True,
                exclude_ids=self.excluded_ids
            )
        else:
            iou_class, accuracy_class, mIoU, mAcc, allAcc = self.sam.get_metrics()

        if self.render_confusion_matrix:
            self.cmr.render()
        logger.info(self.dataset_name + ' ' + self.args.model_path)
        logger.info('Eval result: mIoU/mAcc/allAcc {:.4f}/{:.4f}/{:.4f}.'.format(mIoU, mAcc, allAcc))

        for i in range(self.num_eval_classes):
            if not self.args.taxonomy == 'universal':
                logger.info('Class_{} result: iou/accuracy {:.4f}/{:.4f}, name: {}.'.format(f'{i:02}', iou_class[i], accuracy_class[i], self.class_names[i]))


    # def cal_acc_for_relabeled_model(self, data_list, data_list_relabeled, pred_folder, demo=True) -> None:
    #     """ Calculate accuracy.

    #         Args:
    #         -   data_list: 
    #         -   pred_folder: 
    #         -   class_names: 

    #         Returns:
    #         -   None
    #     """
    #     for i, ((image_path, target_path), (_, target_path_relabeled)) in enumerate(zip(data_list, data_list_relabeled)):
    #         if self.args.img_name_unique:
    #             image_name = Path(image_path).stem
    #         else:
    #             image_name = get_unique_stem_from_last_k_strs(image_path)

    #         pred = cv2.imread(os.path.join(pred_folder, image_name+'.png'), cv2.IMREAD_GRAYSCALE)

    #         target_img = imageio.imread(target_path)
    #         target_img = target_img.astype(np.int64)

    #         target_img_relabeled = imageio.imread(target_path_relabeled)
    #         target_img_relabeled = target_img_relabeled.astype(np.int64)

    #         target_img = self.convert_label_to_pred_taxonomy(target_img)
    #         # construct a "correct" target image here: if pixel A is relabeled as pixel B, and prediction is B, then map prediction B back to A

    #         relabeled_pixels = (target_img_relabeled != target_img)

    #         correct_pixels = (pred == target_img_relabeled)

    #         correct_relabeled_pixels = relabeled_pixels * correct_pixels

    #         pred_final = np.where(correct_relabeled_pixels, target_img, pred)
    #         accuracy_before = (pred == target_img).sum()/target_img.size
    #         accuracy_after = (pred_final == target_img).sum()/target_img.size
    #         print(np.sum(target_img_relabeled == target_img)/target_img.size, accuracy_before, accuracy_after)

    #         # pred[correct_pixels]

    #         sam.update_metrics_cpu(pred_final, target_img, self.pred_dim)


    #         if (i+1) % self.args.vis_freq == 0:
    #             logger.info('Evaluating {0}/{1} on image {2}, accuracy {3:.4f}.'.format(i + 1, len(data_list), image_name+'.png', sam.accuracy))

    #         if demo: 
    #             if (i+1) % self.args.vis_freq == 0:
    #                 mask_save_dir = pred_folder.replace('gray', 'rgb_mask_predictions')
    #                 grid_save_fpath = f'{mask_save_dir}/{image_name}.png'
    #                 rgb_img = cv2_imread_rgb(image_path)
    #                 save_pred_vs_label_7tuple(rgb_img, pred, target_img, id_to_class_name_map, grid_save_fpath)


    def dump_acc_results_to_file(self) -> None:
        """
        Save per-class IoUs and mIoU to a .txt file.
        """
        result_file = f'{self.save_folder}/results.txt'
        if self.args.taxonomy == 'universal':
            iou_class, accuracy_class, mIoU, mAcc, allAcc = self.sam.get_metrics(exclude=True, exclude_ids=self.excluded_ids)
        else:
            iou_class, accuracy_class, mIoU, mAcc, allAcc = self.sam.get_metrics()
        result = open(result_file, 'w')
        result.write('Eval result: mIoU/mAcc/allAcc {:.4f}/{:.4f}/{:.4f}.\n'.format(mIoU, mAcc, allAcc))

        for i in range(self.num_eval_classes):
            if self.args.taxonomy == 'universal':
                if i not in self.excluded_ids:
                    result.write('Class_{} result: iou/accuracy {:.4f}/{:.4f}, name: {}.\n'.format(f'{i:02}', iou_class[i], accuracy_class[i], self.class_names[i]))
            else:
                result.write('Class_{} result: iou/accuracy {:.4f}/{:.4f}, name: {}.\n'.format(f'{i:02}', iou_class[i], accuracy_class[i], self.class_names[i]))
        result.close()

