import torch
from torch import nn
from torch.nn.functional import one_hot, binary_cross_entropy
from torchvision.ops.boxes import box_iou
from nano.models.assigners.gfocal_loss import quality_focal_loss
from nano.ops.box2d import completely_box_iou


def iou_loss(input, target, reduction="mean"):
    """
    calculate IoU loss with both shaped as xyxy
    """
    assert input.shape == target.shape
    loss = 1 - completely_box_iou(input, target)
    if reduction == "mean":
        loss = loss.mean()
    elif reduction == "sum":
        loss = loss.sum()
    return loss


def label_smoothing(target, eps, num_classes, inplace=False):
    if not inplace:
        target = target.clone()
    label_indexes = target > 0
    target[label_indexes] = 1 - eps
    target[~label_indexes] = eps / (num_classes - 1)
    return target


def compute_loss(box_pred, quality_pred, box_target, quality_target, device):
    """
    compute loss from collected bbox, quality targets,
    all anchors are flatten to batch dimension.
    box is processed with GIoU loss,
    quality is processed with QFL loss,

    * Note that preds should be logits. (do not need to sigmoid())
    * specially, obj loss is valid on ALL anchors.

    returns:
        loss: loss for backward
        detached_loss: detached loss, for printing usage
    """
    # bbox regression loss, objectness loss, classification loss (batched)
    loss = torch.zeros(2, device=device)
    lbox = iou_loss(box_pred, box_target, reduction="mean")
    lqfl = 0.4 * quality_focal_loss(quality_pred, quality_target, beta=2, reduction="sum") / max(box_target.size(0), 1)
    loss += torch.stack((lbox, lqfl))
    # loss, loss items (for printing)
    return lbox + lqfl, loss.detach()


class SimOTA(nn.Module):
    """
    https://github.com/Megvii-BaseDetection/YOLOX/blob/0cce4a6f4ed6b7772334a612cdcc51aa16eb0591/yolox/models/yolo_head.py#L425
    https://blog.csdn.net/Megvii_tech/article/details/120030518
    optimize with https://zhuanlan.zhihu.com/p/405789762?ivk_sa=1024320u
    * Takes about (TODO: update this)680MiB CUDA Memory on batch_size=16
    """

    def __init__(self, num_classes, compute_loss=True):
        super().__init__()
        self.num_classes = num_classes
        self.compute_loss = compute_loss
        self.max_topk = 1

    def forward(self, input, target):
        self.device = target.device
        pred, grid_mask, stride_mask = input
        match_mask, box_target, obj_target, cls_target = self.assign_batch(pred, target, grid_mask, stride_mask)
        torch.cuda.empty_cache()
        if self.compute_loss:
            pred = pred.flatten(0, 1)
            box_pred = pred[match_mask, :4]
            quality_pred = pred[:, 4:]
            loss, detached_loss = compute_loss(box_pred, quality_pred, box_target, (cls_target, obj_target), self.device)
            return loss, detached_loss
        else:
            return match_mask, box_target, obj_target, cls_target

    @torch.no_grad()
    def assign_batch(self, pred, target, grid_mask, stride_mask):
        """
        (Tuple) input:
            pred: N - A - < (abs)xyxy | (sigmoid) objectness, c1, c2, ... >
            grid_mask: N - A - 2
            stride_mask: N - A
        target: N(collate) - < collate_id, cid, (abs)xyxy >
        """
        # collect info
        match_mask = []
        box_target = []
        obj_target = []
        cls_target = []
        batch_size = pred.size(0)
        # process per image
        for bi in range(batch_size):
            # process batch ----------------------------------------------------------------
            # get targets & preds batch
            index_per_image = target[:, 0] == bi
            target_per_image = target[index_per_image]
            pred_per_image = pred[bi]

            if target_per_image.size(0) == 0:  # no targets alive
                box_t = pred_per_image.new_zeros((0, 4))
                obj_t = pred_per_image.new_zeros(pred_per_image.size(0))
                cls_t = pred_per_image.new_zeros(pred_per_image.size(0))
                m = pred_per_image.new_zeros(pred_per_image.size(0)).bool()
                box_target.append(box_t)
                obj_target.append(obj_t)
                cls_target.append(cls_t)
                match_mask.append(m)
                continue

            in_box, in_box_center = self.center_sampling(pred_per_image, target_per_image, grid_mask, stride_mask)
            mp, tp, p_iou = self.dynamic_topk(pred_per_image, target_per_image, in_box, in_box_center)

            # match mask
            m = in_box.clone()
            m[in_box] = mp

            # box target
            box_t = target_per_image[tp, 2:]

            # obj target
            obj_t = m.clone().float()
            obj_t[m] *= p_iou[mp]

            # cls target, background = num_classes
            cls_t = pred_per_image.new_ones(pred_per_image.size(0)) * self.num_classes
            cls_t[m] = target_per_image[tp, 1].to(cls_t.dtype)

            # collect mask, box_t, obj_t
            match_mask.append(m)
            box_target.append(box_t)
            obj_target.append(obj_t)
            cls_target.append(cls_t)
            # batch finished --------------------------------------------------------------

        # collect assigned batch
        match_mask = torch.cat(match_mask, 0)
        box_target = torch.cat(box_target, 0)
        obj_target = torch.cat(obj_target, 0)
        cls_target = torch.cat(cls_target, 0)
        return match_mask, box_target, obj_target, cls_target

    @torch.no_grad()
    def center_sampling(self, preds_per_image, targets_per_image, grid_mask, stride_mask):
        """
        perform center sampling for targets.
        returns:
            is_in_boxes_anchor: (A,)
            is_in_boxes_and_center: (T, Am)
        """
        # build match_matrix with shape (num_targets, num_grids)
        T = targets_per_image.size(0)
        A = preds_per_image.size(0)
        # set positive samples (anchors inside bbox or 5x5 of box_center)
        # assert center is in box
        x_centers_per_image = (grid_mask[..., 0] + 0.5).repeat(T, 1) * stride_mask  # (1, na) -> (nt, na)
        y_centers_per_image = (grid_mask[..., 1] + 0.5).repeat(T, 1) * stride_mask  # (1, na) -> (nt, na)
        bboxes_x1_per_image = targets_per_image[..., 2].unsqueeze(1).repeat(1, A)  # (nt, 1) -> (nt, na)
        bboxes_y1_per_image = targets_per_image[..., 3].unsqueeze(1).repeat(1, A)  # (nt, 1) -> (nt, na)
        bboxes_x2_per_image = targets_per_image[..., 4].unsqueeze(1).repeat(1, A)  # (nt, 1) -> (nt, na)
        bboxes_y2_per_image = targets_per_image[..., 5].unsqueeze(1).repeat(1, A)  # (nt, 1) -> (nt, na)
        b_l = x_centers_per_image - bboxes_x1_per_image
        b_t = y_centers_per_image - bboxes_y1_per_image
        b_r = bboxes_x2_per_image - x_centers_per_image
        b_b = bboxes_y2_per_image - y_centers_per_image
        bbox_deltas = torch.stack([b_l, b_t, b_r, b_b], 2)
        is_in_boxes = bbox_deltas.min(dim=-1).values > 0.0
        is_in_boxes_all = is_in_boxes.sum(dim=0) > 0
        # assert center is in 5x5 in box_center
        center_radius = 2.5
        bboxes_xc_per_image = (bboxes_x1_per_image + bboxes_x2_per_image) / 2
        bboxes_yc_per_image = (bboxes_y1_per_image + bboxes_y2_per_image) / 2
        c_x = (x_centers_per_image - bboxes_xc_per_image).abs()
        c_y = (y_centers_per_image - bboxes_yc_per_image).abs()
        center_deltas = torch.stack([c_x, c_y], 2)
        is_in_centers = center_deltas.max(dim=-1).values < center_radius * stride_mask
        is_in_centers_all = is_in_centers.sum(dim=0) > 0
        # in boxes and in centers
        is_in_boxes_anchor = is_in_boxes_all | is_in_centers_all
        is_in_boxes_and_center = is_in_boxes[:, is_in_boxes_anchor] & is_in_centers[:, is_in_boxes_anchor]
        return is_in_boxes_anchor, is_in_boxes_and_center

    @torch.no_grad()
    def dynamic_topk(self, input, target, in_box, in_box_center):
        """
        perform dynamic topk algorithm on matched anchors,
        firstly, each target is assigned to k anchors, (according to sum of ranked iou)
        then multiple targets will be purged,
        which means each anchor should have <= 1 targets.
        returns:
            mp: (P,)   matched anchors (bool)
            tp: (P,)   target index of each matched anchor (long)
            p_iou: (P,)  sum iou for each matched anchor (float)
        """
        # dynamic k algorithm
        paired = input[in_box]

        # get (iou+obj*cls)cost for all paired-target
        T = target.size(0)
        P = paired.size(0)

        pair_wise_iou = box_iou(target[..., 2:], paired[..., :4])  # (T, P)
        pair_wise_iou_loss = -torch.log(pair_wise_iou + 1e-8)

        cls_target = one_hot(target[:, 1].to(torch.int64), self.num_classes)
        cls_target = cls_target.float().unsqueeze(1).repeat(1, P, 1)
        cls_pred = paired[..., 4:].sigmoid().float().unsqueeze(0).repeat(T, 1, 1)
        with torch.cuda.amp.autocast(enabled=False):
            pair_wise_cls_loss = binary_cross_entropy(cls_pred, cls_target, reduction="none").sum(-1)  # (T, P)

        cost = pair_wise_cls_loss + 3.0 * pair_wise_iou_loss + 100000.0 * (~in_box_center)
        del cls_target, cls_pred, pair_wise_iou_loss, pair_wise_cls_loss

        # get dynamic topk
        matching_matrix = torch.zeros_like(cost, dtype=torch.uint8).to(self.device)  # (T, P)
        n_candidate_k = min(10, P)
        topk_ious, _ = torch.topk(pair_wise_iou, n_candidate_k, dim=1)
        dynamic_ks = torch.clamp(topk_ious.sum(1).int(), min=1)
        self.max_topk = float(topk_ious.sum(1).mean().item())  # record dynamic K
        dynamic_ks = dynamic_ks.tolist()

        # select topk paired pred
        for t in range(T):
            _, p = torch.topk(cost[t], k=dynamic_ks[t], largest=False)
            matching_matrix[t, p] = 1
        del topk_ious, dynamic_ks, p

        # purge duplicated assignment
        targets_per_anchor = matching_matrix.sum(0)
        if (targets_per_anchor > 1).sum() > 0:
            _, cost_argmin = torch.min(cost[:, targets_per_anchor > 1], dim=0)
            matching_matrix[:, targets_per_anchor > 1] *= 0
            matching_matrix[cost_argmin, targets_per_anchor > 1] = 1

        # collect results
        mp = matching_matrix.sum(0) > 0  # (P, )
        tp = matching_matrix[:, mp].argmax(0)  # (P, )
        p_iou = pair_wise_iou.max(0).values  # (P, )
        del pair_wise_iou, cost
        return mp, tp, p_iou
