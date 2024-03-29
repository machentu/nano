import onnx
from onnx import numpy_helper
import caffe

from .layers._layer import parse_attribute
from .layers.channel_shuffle import ShuffleChannel
from .layers.resize import Resize
from .layers.constant import CaffeInput
from .layers.convolution import Conv2d
from .layers.activation import Relu, LeakyRelu, Relu6
from .layers.concat import Concat, constant_concat_killer
from .layers.pooling import Maxpool2d, Avgpool2d, GlobalAvgpool2d
from .layers.ffop import Add


__iid = 0


def iid(op_type):
    global __iid
    ret = f"{op_type}_{__iid}"
    __iid += 1
    return ret


def register_shape(layer, shape_dict, input_mode=False):
    output_names = layer.output_names
    if input_mode:
        output_shapes = layer.reshape()
    else:
        input_names = layer.input_names
        bottom_shapes = [shape_dict[x] for x in input_names]
        output_shapes = layer.reshape(bottom_shapes)
    assert len(output_names) == len(output_shapes), (output_names, output_shapes)
    for blob, shape in zip(output_names, output_shapes):
        shape_dict[blob] = tuple(shape)


def export(onnx_path):
    prototxt_path = onnx_path.replace(".onnx", ".prototxt")
    caffemodel_path = onnx_path.replace(".onnx", ".caffemodel")
    graph = onnx.load(onnx_path).graph
    # model = shape_inference.infer_shapes(model)
    constant_dict = {str(n.output[0]): parse_attribute(n)["value"] for n in graph.node if n.op_type == "Constant"}
    tensor_dict = {t.name: numpy_helper.to_array(t) for t in graph.initializer}
    shape_dict = {}
    layer_list = []
    constant_concat_killer(graph, constant_dict, tensor_dict)
    # append input nodes
    for node in graph.input:
        if node.name in tensor_dict:
            continue
        layer = CaffeInput(node)
        register_shape(layer, shape_dict, input_mode=True)
        layer_list.append(layer)
    # append main nodes
    for node in graph.node:
        if node.op_type == "Constant":
            continue
        node.name = iid(node.op_type)
        print(f"processing node [{node.name}]")
        if node.op_type == "Conv":
            layer = Conv2d(node, tensor_dict, shape_dict)
        elif node.op_type == "Relu":
            layer = Relu(node)
        elif node.op_type == "LeakyRelu":
            layer = LeakyRelu(node)
        elif node.op_type == "Concat":
            layer = Concat(node)
        elif node.op_type == "MaxPool":
            layer = Maxpool2d(node)
        elif node.op_type == "AveragePool":
            layer = Avgpool2d(node)
        elif node.op_type == "GlobalAvaragePool":
            layer = GlobalAvgpool2d(node)
        elif node.op_type == "Add":
            layer = Add(node, shape_dict)
        elif node.op_type == "Clip":
            layer = Relu6(node, constant_dict)
        elif node.op_type == "Resize":
            layer = Resize(node, tensor_dict, shape_dict)
        elif node.op_type == "Slice":
            layer = Slice(node, constant_dict)
        elif node.op_type == "channel_shuffle":
            layer = ShuffleChannel(node, constant_dict)
        else:
            raise NotImplementedError(f"{node.op_type} not supported.")
        register_shape(layer, shape_dict)
        layer_list.append(layer)

    with open(prototxt_path, "w") as f:
        for layer in layer_list:
            if hasattr(layer, "shadow_proto"):
                proto = layer.shadow_proto()
            else:
                proto = layer.to_proto()
            f.write(proto)

    caffe.set_mode_cpu()
    net = caffe.Net(prototxt_path, caffe.TEST)
    for layer in layer_list:
        if hasattr(layer, "inject_params"):
            layer.inject_params(net)
    net.save(caffemodel_path)

    custom_prototxt_path = prototxt_path.replace(".prototxt", "-custom.prototxt")
    with open(custom_prototxt_path, "w") as f:
        for layer in layer_list:
            proto = layer.to_proto()
            f.write(proto)

    return onnx_path, caffemodel_path, custom_prototxt_path
