import cv2
import imageio
import logging
import numpy as np
import os
from pathlib import Path
import pdb
import time
import torch
import torch.nn as nn
import torch.utils.data
import torch.backends.cudnn as cudnn
from typing import List, Tuple

from mseg.utils.dir_utils import check_mkdir, create_leading_fpath_dirs
from mseg.utils.names_utils import get_universal_class_names
from mseg.utils.mask_utils_detectron2 import Visualizer
from mseg.utils.resize_util import resize_img_by_short_side

from mseg.taxonomy.taxonomy_converter import TaxonomyConverter
from mseg.taxonomy.naive_taxonomy_converter import NaiveTaxonomyConverter

from mseg_semantic.model.pspnet import PSPNet
from mseg_semantic.utils.avg_meter import AverageMeter
from mseg_semantic.utils.normalization_utils import (
    get_imagenet_mean_std,
    normalize_img
)
from mseg_semantic.utils.cv2_video_utils import VideoWriter, VideoReader
from mseg_semantic.utils import dataset, transform, config
from mseg_semantic.utils.img_path_utils import dump_relpath_txt


"""
Given a specified task, run inference on it using a pre-trained network.
Used for demos, and for testing on an evaluation dataset.

If projecting universal taxonomy into a different evaluation taxonomy,
the argmax comes *after* the linear mapping, so that probabilities can be
summed first.

Note: "base size" should be the length of the shorter side of the desired
inference image resolution. Note that the official PSPNet repo 
(https://github.com/hszhao/semseg/blob/master/tool/test.py) treats
base_size as the longer side, which we found less intuitive given
screen resolution is generally described by shorter side length.

"base_size" is a very important parameter and will
affect results significantly.
"""

_ROOT = Path(__file__).resolve().parent.parent.parent

def get_logger():
    """
    """
    logger_name = "main-logger"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    fmt = "[%(asctime)s %(levelname)s %(filename)s line %(lineno)d %(process)d] %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    return logger

logger = get_logger()


def get_unique_stem_from_last_k_strs(fpath: str, k: int = 4) -> str:
    """
        Args:
        -   fpath
        -   k

        Returns:
        -   unique_stem: string
    """
    parts = Path(fpath).parts
    unique_stem = '_'.join(parts[-4:-1]) + '_' + Path(fpath).stem
    return unique_stem


class ToFlatLabel(object):
    def __init__(self, tc_init, dataset):
        self.dataset = dataset
        self.tc = tc_init

    def __call__(self, image, label):
        return image, self.tc.transform_label(label, self.dataset)


def resize_by_scaled_short_side(
    image: np.ndarray,
    base_size: int,
    scale: float
    ) -> np.ndarray:
    """
        Args:
        -    image: Numpy array of shape ()
        -    scale: 

        Returns:
        -    image_scale: 
    """
    h, w, _ = image.shape
    short_size = round(scale * base_size)
    new_h = short_size
    new_w = short_size
    # Preserve the aspect ratio
    if h > w:
        new_h = round(short_size/float(w)*h)
    else:
        new_w = round(short_size/float(h)*w)
    image_scale = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    return image_scale

def pad_to_crop_sz(
    image: np.ndarray,
    crop_h: int,
    crop_w: int,
    mean: Tuple[float,float,float]
    ) -> Tuple[np.ndarray,int,int]:
    """
    Network input should be at least crop size, so we pad using mean values if
    provided image is too small. No rescaling is performed here.

    We use cv2.copyMakeBorder to copy the source image into the middle of a 
    destination image. The areas to the left, to the right, above and below the 
    copied source image will be filled with extrapolated pixels, in this case the 
    provided mean pixel intensity.

        Args:
        -    image:
        -    crop_h: integer representing crop height
        -    crop_w: integer representing crop width

        Returns:
        -    image: Numpy array of shape (crop_h x crop_w) representing a 
                square image, with short side of square is at least crop size.
        -    pad_h_half: half the number of pixels used as padding along height dim
        -    pad_w_half" half the number of pixels used as padding along width dim
    """
    ori_h, ori_w, _ = image.shape
    pad_h = max(crop_h - ori_h, 0)
    pad_w = max(crop_w - ori_w, 0)
    pad_h_half = int(pad_h / 2)
    pad_w_half = int(pad_w / 2)
    if pad_h > 0 or pad_w > 0:
        image = cv2.copyMakeBorder(
            src=image,
            top=pad_h_half,
            bottom=pad_h - pad_h_half,
            left=pad_w_half,
            right=pad_w - pad_w_half,
            borderType=cv2.BORDER_CONSTANT,
            value=mean
        )
    return image, pad_h_half, pad_w_half


def imread_rgb(img_fpath: str) -> np.ndarray:
    """
        Returns:
        -    RGB 3 channel nd-array with shape H * W * 3
    """
    bgr_img = cv2.imread(img_fpath, cv2.IMREAD_COLOR)
    rgb_img = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
    rgb_img = np.float32(rgb_img)
    return rgb_img

class InferenceTask:

    def __init__(self,
        args,
        base_size      : int,
        crop_h         : int,
        crop_w         : int,
        input_file     : str,
        output_taxonomy: str,
        scales         : List[float],
        device_type    : str,
        output_path    : str
        ):
        """
        We always use the ImageNet mean and standard deviation for normalization.
        mean: 3-tuple of floats, representing pixel mean value
        std: 3-tuple of floats, representing pixel standard deviation

        'args' should contain at least two fields (shown below).

            Args:mseg-3m.pth
            -    args:
            -    base_size:
            -    crop_h: integer representing crop height, e.g. 473
            -    crop_w: integer representing crop width, e.g. 473
            -    input_file: could be absolute path to .txt file, .mp4 file,
                    or to a directory full of jpg images
            -    output_taxonomy
            -    scales
            -    use_gpu
        """
        self.args = args
        assert isinstance(self.args.img_name_unique, bool)
        assert isinstance(self.args.print_freq, int)
        assert isinstance(self.args.num_model_classes, int)
        assert isinstance(self.args.model_path, str)
        self.pred_dim = self.args.num_model_classes

        self.base_size = base_size
        self.crop_h = crop_h
        self.crop_w = crop_w
        self.input_file = input_file
        self.output_taxonomy = output_taxonomy
        self.scales = scales
        self.use_gpu = device_type == 'cuda'
        self.device = torch.device(device_type)
        self.output_path = output_path

        self.mean, self.std = get_imagenet_mean_std()
        self.model = self.load_model(args)
        self.softmax = nn.Softmax(dim=1)

        self.gray_folder = None # optional, intended for dataloader use
        self.data_list = None # optional, intended for dataloader use

        if self.output_taxonomy != 'universal':
            assert isinstance(self.args.dataset, str)
            self.dataset_name = args.dataset
            self.tc = TaxonomyConverter()

        if self.args.arch == 'psp':
            assert isinstance(self.args.zoom_factor, int)
            assert isinstance(self.args.network_name, int)

        self.id_to_class_name_map = {
            i: classname for i, classname in enumerate(get_universal_class_names())
        }

        # indicate which scales were used to make predictions
        # (multi-scale vs. single-scale)
        self.scales_str = 'ms' if len(args.scales) > 1 else 'ss'

    def load_model(self, args):
        """
        Load Pytorch pre-trained model from disk of type 
        torch.nn.DataParallel. Note that
        `args.num_model_classes` will be size of logits output.

            Args:
            -   args: 

            Returns:
            -   model
        """
        if args.arch == 'psp':
            model = PSPNet(
            layers=args.layers,
            classes=args.num_model_classes,
            zoom_factor=args.zoom_factor,
            pretrained=False,
            network_name=args.network_name
            )
        elif args.arch == 'hrnet':
            from mseg_semantic.model.seg_hrnet import get_configured_hrnet
            # note apex batchnorm is hardcoded 
            model = get_configured_hrnet(args.num_model_classes, load_imagenet_model=False)
        elif args.arch == 'hrnet_ocr':
            from mseg_semantic.model.seg_hrnet_ocr import get_configured_hrnet_ocr
            model = get_configured_hrnet_ocr(args.num_model_classes)

        model = torch.nn.DataParallel(model)
        #model.to(self.device)

        if os.path.isfile(args.model_path):
            logger.info(f"=> loading checkpoint '{args.model_path}'")
            if self.use_gpu:
                checkpoint = torch.load(args.model_path)
            else:
                checkpoint = torch.load(args.model_path, map_location='cpu')
            model.load_state_dict(checkpoint['state_dict'], strict=False)
            logger.info(f"=> loaded checkpoint '{args.model_path}'")
        else:
            raise RuntimeError(f"=> no checkpoint found at '{args.model_path}'")

        return model

    def execute(self, min_resolution=1080):
        """
        Execute the demo, i.e. feed all of the desired input through the
        network and obtain predictions. Gracefully handles .txt, 
        or video file (.mp4, etc), or directory input.
        """
        logger.info('>>>>>>>>>>>>>>>> Start inference task >>>>>>>>>>>>>>>>')
        self.model.eval()

        """
        Since overlaid class text is difficult to read below 1080p, we upsample
        predictions.
        """
        logger.info(f'Write image prediction to {self.output_path}')

        rgb_img = imread_rgb(self.input_file)
        pred_label_img = self.execute_on_img(rgb_img)

        # avoid blurry images by upsampling RGB before overlaying text
        if np.amin(rgb_img.shape[:2]) < min_resolution:
            rgb_img = resize_img_by_short_side(rgb_img, min_resolution, 'rgb')
            pred_label_img = resize_img_by_short_side(pred_label_img, min_resolution, 'label')

        imageio.imwrite(self.output_path, pred_label_img)

        logger.info('<<<<<<<<<<<<<<<<< Inference task completed <<<<<<<<<<<<<<<<<')

    def execute_on_img(self, image: np.ndarray) -> np.ndarray:
        """
        Rather than feeding in crops w/ sliding window across the full-res image, we 
        downsample/upsample the image to a default inference size. This may differ
        from the best training size.

        For example, if trained on small images, we must shrink down the image in 
        testing (preserving the aspect ratio), based on the parameter "base_size",
        which is the short side of the image.

            Args:
            -    image: Numpy array representing RGB image
            
            Returns:
            -    gray_img: prediction, representing predicted label map
        """
        h, w, _ = image.shape

        prediction = np.zeros((h, w, self.pred_dim), dtype=float)
        prediction = torch.Tensor(prediction).to(self.device)

        for scale in self.scales:
            image_scale = resize_by_scaled_short_side(image, self.base_size, scale)
            prediction = prediction + torch.Tensor(self.scale_process_cuda(image_scale, h, w)).to(self.device)

        prediction /= len(self.scales)
        prediction = torch.argmax(prediction, axis=2)
        prediction = prediction.data.cpu().numpy()
        gray_img = np.uint8(prediction)
        return gray_img

    def scale_process_cuda(self, image: np.ndarray, h: int, w: int, stride_rate: float = 2/3):
        """ First, pad the image. If input is (384x512), then we must pad it up to shape
        to have shorter side "scaled base_size". 

        Then we perform the sliding window on this scaled image, and then interpolate 
        (downsample or upsample) the prediction back to the original one.

        At each pixel, we increment a counter for the number of times this pixel
        has passed through the sliding window.

        Args:
        -   image: Array, representing image where shortest edge is adjusted to base_size
        -   h: integer representing raw image height, e.g. for NYU it is 480
        -   w: integer representing raw image width, e.g. for NYU it is 640
        -   stride_rate

        Returns:
        -   prediction: predictions with shorter side equal to self.base_size
        """
        start1 = time.time()                

        ori_h, ori_w, _ = image.shape
        image, pad_h_half, pad_w_half = pad_to_crop_sz(image, self.crop_h, self.crop_w, self.mean)
        new_h, new_w, _ = image.shape
        stride_h = int(np.ceil(self.crop_h*stride_rate))
        stride_w = int(np.ceil(self.crop_w*stride_rate))
        grid_h = int(np.ceil(float(new_h-self.crop_h)/stride_h) + 1)
        grid_w = int(np.ceil(float(new_w-self.crop_w)/stride_w) + 1)

        prediction_crop = torch.zeros((self.pred_dim, new_h, new_w)).to(self.device)
        count_crop = torch.zeros((new_h, new_w)).to(self.device)

        for index_h in range(0, grid_h):
            for index_w in range(0, grid_w):
                s_h = index_h * stride_h
                e_h = min(s_h + self.crop_h, new_h)
                s_h = e_h - self.crop_h
                s_w = index_w * stride_w
                e_w = min(s_w + self.crop_w, new_w)
                s_w = e_w - self.crop_w
                image_crop = image[s_h:e_h, s_w:e_w].copy()
                count_crop[s_h:e_h, s_w:e_w] += 1
                prediction_crop[:, s_h:e_h, s_w:e_w] += self.net_process(image_crop)

        prediction_crop /= count_crop.unsqueeze(0)
        # disregard predictions from padded portion of image
        prediction_crop = prediction_crop[:, pad_h_half:pad_h_half+ori_h, pad_w_half:pad_w_half+ori_w]

        # CHW -> HWC
        prediction_crop = prediction_crop.permute(1,2,0)
        prediction_crop = prediction_crop.data.cpu().numpy()

        # upsample or shrink predictions back down to scale=1.0
        prediction = cv2.resize(prediction_crop, (w, h), interpolation=cv2.INTER_LINEAR)

        return prediction


    def net_process(self, image: np.ndarray, flip: bool = True):
        """ Feed input through the network.

            In addition to running a crop through the network, we can flip
            the crop horizontally, run both crops through the network, and then
            average them appropriately.

            Args:
            -   model:
            -   image:
            -   flip: boolean, whether to average with flipped patch output

            Returns:
            -   output:
        """
        input = torch.from_numpy(image.transpose((2, 0, 1))).float().to(self.device)
        normalize_img(input, self.mean, self.std)
        input = input.unsqueeze(0)

        if flip:
            # add another example to batch dimension, that is the flipped crop
            input = torch.cat([input, input.flip(3)], 0)
        with torch.no_grad():
            output = self.model(input)
        _, _, h_i, w_i = input.shape
        _, _, h_o, w_o = output.shape
        if (h_o != h_i) or (w_o != w_i):
            output = F.interpolate(output, (h_i, w_i), mode='bilinear', align_corners=True)

        if self.output_taxonomy == 'universal':
            output = self.softmax(output)
        elif self.output_taxonomy == 'test_dataset':
            output = self.convert_pred_to_label_tax_and_softmax(output)
        else:
            print('Unrecognized output taxonomy. Quitting....')
            quit()

        if flip:
            # take back out the flipped crop, correct its orientation, and average result
            output = (output[0] + output[1].flip(2)) / 2
        else:
            output = output[0]

        return output

    def convert_pred_to_label_tax_and_softmax(self, output):
        """
        """
        if not self.args.universal:
            output = self.tc.transform_predictions_test(output, self.args.dataset)
        else:
            output = self.tc.transform_predictions_universal(output, self.args.dataset)
        return output
