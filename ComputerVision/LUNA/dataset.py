from collections import namedtuple
import functools
import csv
import glob
import copy
import os
import math
import random

import numpy as np
import SimpleITK as sitk

import torch
from torch.utils.data import Dataset
import torch.nn.functional as F

from util.util import xyz_tuple, xyz_to_irc
from util.disk import get_cache
from util.loggingconf import logging

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

# params
width_irc = (32, 48, 48)                # the size of subset of voxels around the center of nodule we want to retrieve
data_root_path = 'C:/MLData/LUNA/'       # root folder of where the LUNA dataset is stored
raw_cache = get_cache(data_root_path + 'cache/')


# a sample of data contains whether it's nodule, its diamter (mm), its CT uid, and its center (xyz) in the CT
nodule_candidate_info_tuple = namedtuple("nodule_candidate_info_tuple", "is_nodule, diameter, series_uid, center")

# function to populate the data with samples of (is_nodule, diameter, uid, center)
@functools.lru_cache(maxsize=1)                         # cache since data file parsing could be slow, and we use this function often
def get_nodule_candidate_info_list():

    # get the data files that are present on disk (for experimenting with smaller dataset)
    mhd_list = glob.glob(data_root_path + 'subset*/*.mhd')
    uid_on_disk = {os.path.split(p)[-1][:-4] for p in mhd_list}


    # get data from annotations.csv in the form of {uid: (center, diameter)}
    diamter_dict = {}
    with open(data_root_path + 'annotations.csv', 'r') as f:
        # read each row, skip header
        for row in list(csv.reader(f))[1:]:
            series_uid = row[0]
            annotation_center = tuple([float(value) for value in row[1:4]])
            annotation_diameter = float(row[4])
            
            diamter_dict.setdefault(series_uid, []).append((annotation_center, annotation_diameter))


    # get data from candidates.csv and construct data of (is_nodule, diameter, uid, center)   
    nodule_candidate_info_list = []
    with open(data_root_path + 'candidates.csv', 'r') as f:
        # read each row, skip header
        for row in list(csv.reader(f))[1:]:
            series_uid = row[0]

            # if the uid in the annotation file does not correspond to a CT data on disk
            if series_uid not in uid_on_disk:
                continue

            is_nodule = bool(int(row[4]))
            candidate_center = tuple([float(value) for value in row[1:4]])

            # the center in the annotation file and the candidate files are different, try to match them if close enough, so we can set each sample with its diameter
            candidate_diameter = 0.0    # if don't see a corresponding sample in the annotations, set the sample's diameter to 0
            for annotation in diamter_dict.get(series_uid, []):
                annotation_center_xyz, annotation_diameter_mm = annotation

                # check if the center between the two files are close enough
                for i in range(3):
                    delta_mm = abs(candidate_center[i] - annotation_center_xyz[i])
                    if(delta_mm > annotation_diameter_mm / 4):
                        break                                       # not close, set this sample's diameter to 0 (only using the ordered diameter to split into train and val set, so could be ok)
                else:
                    candidate_diameter = annotation_diameter_mm     # close, set the sample's diameter from the annotation file
                    break

            nodule_candidate_info_list.append(nodule_candidate_info_tuple(
                is_nodule,
                candidate_diameter,
                series_uid,
                candidate_center
            ))

    # sort from is_nodule and diameter first
    nodule_candidate_info_list.sort(reverse=True)
    return nodule_candidate_info_list


class CtScan:

    def __init__(self, series_uid):
        # reads a CT scan metadata and raw image file, and convert to np array
        mhd_path = glob.glob(data_root_path + f'subset*/{series_uid}.mhd')[0]
        ct_mhd = sitk.ReadImage(mhd_path)                                       # sitk reads both metadata and .raw file
        ct_np = np.array(sitk.GetArrayFromImage(ct_mhd), dtype=np.float32)      # 3D array of CT scan

        # clip the CT scan HU values between -1000 (think empty air) to 1000 (think bone), as to remove outliers
        ct_np.clip(-1000, 1000, ct_np)

        self.series_uid = series_uid
        self.ct = ct_np

        self.origin_xyz = xyz_tuple(*ct_mhd.GetOrigin())                # get annotation coord origin from metadata
        self.vx_size_xyz = xyz_tuple(*ct_mhd.GetSpacing())              # get CT scan voxel size (in mm) from metadata
        self.direction = np.array(ct_mhd.GetDirection()).reshape(3, 3)  # get the transformation matrix

    # function to extract some CT voxels around the specified candidate center
    def get_raw_candidate(self, center_xyz, width_irc):
        # convert the center from XYZ to IRC
        center_irc = xyz_to_irc(center_xyz, self.origin_xyz, self.vx_size_xyz, self.direction)

        slice_list = []
        # iterate over each axis
        for axis, center in enumerate(center_irc):
            # check that the center is within bound
            assert center >=0 and center < self.ct.shape[axis], repr('candidate center out of bound error')

            # set the start and end index of voxels to include based on the specified width
            start_idx = int(round(center - width_irc[axis]/2))
            end_idx = int(start_idx + width_irc[axis])

            # cap start_idx = 0, if < 0
            if start_idx < 0:
                start_idx = 0
                end_idx = int(width_irc[axis])
            # cap end_idx to bound
            if end_idx > self.ct.shape[axis]:
                end_idx = self.ct.shape[axis]
                start_idx = int(end_idx - width_irc[axis])

            # include these voxels
            slice_list.append(slice(start_idx, end_idx))

        ct_chunk = self.ct[tuple(slice_list)]
        # return the subset of voxels around the center, and center
        return ct_chunk, center_irc
    
@functools.lru_cache(maxsize=1, typed=True)     # cache because loading CT is slow, and we will access often
def get_ct(series_uid):
    return CtScan(series_uid)

# function to load CT scans, extract voxels around nodule, and cache to disk
@raw_cache.memoize(typed=True)                  # cache on disk because loading CT is slow, and we will access often
def get_ct_raw_candidate(series_uid, center_xyz, width_irc):
    # get the ct scan
    ct = get_ct(series_uid)
    # get the ct scan's voxel around the center of the nodule candidate
    ct_chunk, center_irc = ct.get_raw_candidate(center_xyz, width_irc)
    return ct_chunk, center_irc

def get_augmented_ct_candidate(augmentations, uid, center_xyz, width_irc):
    # get the unaugmented data
    ct_chunk, center_irc = get_ct_raw_candidate(uid, center_xyz, width_irc)

    # convert to tensor
    ct = torch.tensor(ct_chunk).unsqueeze(0).unsqueeze(0).to(torch.float32) # add a batch and channel dimension, now (batch, channel, index, row, column)

    # 3D transformation matrix
    transform = torch.eye(4)    # init to diagonal matrix, i.e. no transformation

    for i in range(3):  # 3 because IRC dimensions
        # random flip in any axis
        if 'flip' in augmentations:
            if random.random() > 0.5:
                transform[i, i] *= -1

        # random shifting by a few voxels
        if 'shift' in augmentations:
            offset_value = augmentations['shift']
            shifting_scale = random.random() * 2 - 1
            transform[i, 3] = offset_value * shifting_scale   # translation is the 4th column in a transformation matrix

        # random scale
        if 'scale' in augmentations:
            scale_value = augmentations['scale']
            random_scale_factor = random.random() * 2 - 1
            transform[i, i] *= 1.0 + scale_value * random_scale_factor

    if 'rotate' in augmentations:
        # random rotation angle
        angle_rad = random.random() * math.pi * 2

        s = math.sin(angle_rad)
        c = math.cos(angle_rad)

        # only rotate in the xy dimension (because z dimension has a different scale in the CT, rotating might stretch it)
        rotation = torch.tensor([
            [c, -s, 0, 0],
            [s, c, 0, 0],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ])

        # rotation matrix multiplication
        transform @= rotation

    # augment the sample using the transformation matrix
    affine = F.affine_grid(transform[:3].unsqueeze(0).to(torch.float32), ct.size(), align_corners=False)
    augmented_ct = F.grid_sample(ct, affine, padding_mode='border', align_corners=False).to('cpu')

    # add random noise
    if 'noise' in augmentations:
        noise = torch.randn_like(augmented_ct)
        noise *= augmentations['noise']
        augmented_ct += noise

    return augmented_ct[0], center_irc


class LunaDataset(Dataset):

    def __init__(self, val_stride=10, is_val_set=None, series_uid=None, ratio=0, augmentations=None):
        super().__init__()

        # ratio of neg to pos samples, e.g. if ratio is 1, then 1:1, if 2, then 2:1
        self.ratio = ratio

        # use training data augmentation?
        self.augmentations = augmentations

        # get all the data samples annotations
        self.nodule_candidate_info = copy.copy(get_nodule_candidate_info_list())    # copy so won't alter the cached copy

        # if we only want certain samples as specified by uid
        if series_uid:
            self.nodule_candidate_info = [x for x in self.nodule_candidate_info if x.series_uid == series_uid]

        # if creating the val set
        if is_val_set:
            assert val_stride > 0, val_stride
            # select every nth sample as the val set, as indicated by val_stride
            self.nodule_candidate_info = self.nodule_candidate_info[::val_stride]
        else:   # training set
            assert val_stride > 0
            # remove every nth sample from the training set, as they are in the val set
            del self.nodule_candidate_info[::val_stride]
            assert self.nodule_candidate_info

        # sort the data
        self.nodule_candidate_info.sort(key=lambda x: (x.series_uid, x.center))

        # keep track of positive and negative samples (for returning balanced data batches later)
        self.postive_samples = [x for x in self.nodule_candidate_info if x.is_nodule]
        self.negative_samples = [x for x in self.nodule_candidate_info if not x.is_nodule]

        log.info(f"Dataset {len(self.nodule_candidate_info)} {str('val') if is_val_set else str('train')} samples")

    def __len__(self):
        return len(self.nodule_candidate_info)
    
    def __getitem__(self, idx):
        # if explicitly balancing positive and negative samples
        if self.ratio:
            
            # every nth indice should be a positive sample
            n = self.ratio + 1

            if idx % n:
                # if the current idx should be a negative sample
                neg_idx = idx - 1 - idx // n
                neg_idx %= len(self.negative_samples)
                sample = self.negative_samples[neg_idx]
            else:
                # if the current idx should be a positive sample
                pos_idx = idx // n
                pos_idx %= len(self.postive_samples)
                sample = self.postive_samples[pos_idx]
        else:
            # no balancing, just get the sample's annotation specified by the idx
            sample = self.nodule_candidate_info[idx]


        if self.augmentations:
            # get the sample's center and also voxels around the center (with data augmentation)
            ct_chunk, center_irc = get_augmented_ct_candidate(self.augmentations, sample.series_uid, sample.center, width_irc)
        else:
            # get the sample's center and also voxels around the center
            ct_chunk, center_irc = get_ct_raw_candidate(sample.series_uid, sample.center, width_irc)

            ct_chunk = torch.from_numpy(ct_chunk).to(torch.float32)     # convert to tesnor
            ct_chunk = ct_chunk.unsqueeze(0)                            # add a channel dimension, now (channel, index, row, column)

        # construct label as two class, i.e. not nodule or is nodule
        label = torch.tensor([not sample.is_nodule, sample.is_nodule], dtype=torch.long)

        return ct_chunk, label, sample.series_uid, torch.tensor(center_irc)