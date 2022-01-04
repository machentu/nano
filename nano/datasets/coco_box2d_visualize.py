import numpy as np
import torch
import cv2


def from_tensor_image(x):
    """
    converts tensor to numpy image
    returns:
        np_img: (h, w, bgr)
    """
    np_img = (x * 255.0).int().numpy()
    np_img = np_img[::-1].transpose((1, 2, 0))  # CHW to HWC, RGB to BGR, 0~1 to 0~255
    np_img = np.ascontiguousarray(np_img)
    return np_img


def from_numpy_image(img):
    """
    converts numpy image to tensor
    (inplace-safe function)
    returns:
        x: (rgb, h, w)
    """
    x = img.transpose((2, 0, 1))[::-1]  # HWC to CHW, BGR to RGB
    x = np.ascontiguousarray(x)
    x = torch.from_numpy(x).float() / 255.0  # uint8 to float32, 0-255 to 0.0-1.0
    return x


def draw_bounding_boxes(tensor_im, labels, label_names=("person", "bike", "car"), font=cv2.FONT_HERSHEY_SIMPLEX, font_scale=0.3, font_thickness=1, font_color=(0, 0, 0)) -> None:
    """
    tensor_im: tensor image
    labels: numpy.array or torch.Tensor, (N, cxyxy)
    (inplace-safe function)
    returns:
        cv2_img: cv2 image with bounding boxes
    """
    cv2_img = from_tensor_image(tensor_im)  # CHW BGR 0~255
    if len(labels) > 0:
        for box in labels:
            cid, x1, y1, x2, y2 = [int(x) for x in box]
            color = list(np.random.random(size=3) * 128 + 128)
            cv2.rectangle(cv2_img, (x1, y1), (x2, y2), color, 1, 4, 0)
            label = label_names[cid]
            text_size, _ = cv2.getTextSize(label, font, font_scale, font_thickness)
            text_w, text_h = text_size
            cv2.rectangle(cv2_img, (x1, y1), (x1 + text_w + 2, y1 + text_h + 2), color, -1)
            cv2.putText(cv2_img, label, (x1, y1 + text_h), font, font_scale, font_color, font_thickness)
    return cv2_img
