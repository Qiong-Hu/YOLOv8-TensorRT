from typing import Tuple

import torch
import torch.nn as nn
from torch import Graph, Tensor, Value


def make_anchors(feats: Tensor,
                 strides: Tensor,
                 grid_cell_offset: float = 0.5) -> Tuple[Tensor, Tensor]:
    anchor_points, stride_tensor = [], []
    assert feats is not None
    dtype, device = feats[0].dtype, feats[0].device
    for i, stride in enumerate(strides):
        _, _, h, w = feats[i].shape
        sx = torch.arange(end=w, device=device,
                          dtype=dtype) + grid_cell_offset  # shift x
        sy = torch.arange(end=h, device=device,
                          dtype=dtype) + grid_cell_offset  # shift y
        sy, sx = torch.meshgrid(sy, sx)
        anchor_points.append(torch.stack((sx, sy), -1).view(-1, 2))
        stride_tensor.append(
            torch.full((h * w, 1), stride, dtype=dtype, device=device))
    return torch.cat(anchor_points), torch.cat(stride_tensor)


class TRT_NMS(torch.autograd.Function):

    @staticmethod
    def forward(
            ctx: Graph,
            boxes: Tensor,
            scores: Tensor,
            iou_threshold: float = 0.65,
            score_threshold: float = 0.25,
            max_output_boxes: int = 100,
            background_class: int = -1,
            box_coding: int = 0,
            plugin_version: str = '1',
            score_activation: int = 0
    ) -> Tuple[Tensor, Tensor, Tensor, Tensor]:
        batch_size, num_boxes, num_classes = scores.shape
        num_det = torch.randint(0,
                                max_output_boxes, (batch_size, 1),
                                dtype=torch.int32)
        det_boxes = torch.randn(batch_size, max_output_boxes, 4)
        det_scores = torch.randn(batch_size, max_output_boxes)
        det_classes = torch.randint(0,
                                    num_classes,
                                    (batch_size, max_output_boxes),
                                    dtype=torch.int32)

        return num_det, det_boxes, det_scores, det_classes

    @staticmethod
    def symbolic(
            g,
            boxes: Value,
            scores: Value,
            iou_threshold: float = 0.45,
            score_threshold: float = 0.25,
            max_output_boxes: int = 100,
            background_class: int = -1,
            box_coding: int = 0,
            score_activation: int = 0,
            plugin_version: str = '1') -> Tuple[Value, Value, Value, Value]:
        out = g.op('TRT::EfficientNMS_TRT',
                   boxes,
                   scores,
                   iou_threshold_f=iou_threshold,
                   score_threshold_f=score_threshold,
                   max_output_boxes_i=max_output_boxes,
                   background_class_i=background_class,
                   box_coding_i=box_coding,
                   plugin_version_s=plugin_version,
                   score_activation_i=score_activation,
                   outputs=4)
        nums_dets, boxes, scores, classes = out
        return nums_dets, boxes, scores, classes


class C2f(nn.Module):

    def __init__(self,
                 c1,
                 c2,
                 n=1,
                 shortcut=False,
                 g=1,
                 e=0.5):  # ch_in, ch_out, number, shortcut, groups, expansion
        super().__init__()

    def forward(self, x):
        y = list(self.cv1(x).split((self.c, self.c), 1))
        x = self.cv1(x)
        y = [x, x[:, self.c:, ...]]
        y.extend(m(y[-1]) for m in self.m)
        y.pop(1)
        return self.cv2(torch.cat(y, 1))


class PostDetect(nn.Module):
    export = True
    shape = None
    dynamic = False
    iou_thres = 0.65
    conf_thres = 0.25
    topk = 100

    def __init__(self, nc=80, ch=()):
        super().__init__()

    def forward(self, x):
        shape = x[0].shape
        b, res = shape[0], []
        for i in range(self.nl):
            res.append(torch.cat((self.cv2[i](x[i]), self.cv3[i](x[i])), 1))
        if self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(
                0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape
        x = [i.view(b, self.no, -1) for i in res]
        y = torch.cat(x, 2)
        box, cls = y[:, :self.reg_max * 4, ...], y[:, self.reg_max * 4:,
                                                   ...].sigmoid()
        box = box.view(b, 4, self.reg_max, -1).permute(0, 1, 3, 2).contiguous()
        box = box.softmax(-1) @ torch.arange(self.reg_max).to(box)
        box0, box1 = -box[:, :2, ...], box[:, 2:, ...]
        box = self.anchors.repeat(b, 2, 1) + torch.cat([box0, box1], 1)
        box = box * self.strides

        return TRT_NMS.apply(box.transpose(1, 2), cls.transpose(1, 2),
                             self.iou_thres, self.conf_thres, self.topk)


def optim(module: nn.Module):
    s = str(type(module))[6:-2].split('.')[-1]
    if s == 'Detect':
        setattr(module, '__class__', PostDetect)
    elif s == 'C2f':
        setattr(module, '__class__', C2f)