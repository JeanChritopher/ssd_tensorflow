import numpy as np
import tensorflow as tf
import vgg.ssd_base as vgg16
import loaderutil
import tf_common as tfc
import constants as c
from constants import layer_boxes
import skimage.transform
import matcher

FLAGS = tf.app.flags.FLAGS

class SSD:
    def __init__(self, model_dir=None, gpu_fraction=0.7):
        config = tf.ConfigProto(allow_soft_placement=True)
        config.gpu_options.per_process_gpu_memory_fraction = gpu_fraction

        self.sess = tf.Session(config=config)

        if model_dir is None:
            model_dir = FLAGS.model_dir

        ckpt = tf.train.get_checkpoint_state(model_dir)

        def init_model():
            self.imgs_ph, self.bn, self.output_tensors, self.pred_labels, self.pred_locs, self.pred_labels_amax, self.pred_labels_argmax, self.segout\
                = model(self.sess)
            total_boxes = self.pred_labels.get_shape().as_list()[1]
            self.positives_ph, self.negatives_ph, self.true_labels_ph, self.true_locs_ph, self.true_seg_ph,\
            self.total_loss, self.class_loss, self.loc_loss, self.seg_loss = \
                loss(self.pred_labels, self.pred_locs, self.segout, total_boxes)
            out_shapes = [out.get_shape().as_list() for out in self.output_tensors]
            c.out_shapes = out_shapes
            c.defaults = default_boxes(out_shapes)

            # variables in model are already initialized, so only initialize those declared after
            with tf.variable_scope("optimizer"):
                self.global_step = tf.Variable(0)
                self.lr_ph = tf.placeholder(tf.float32, shape=[])

                self.optimizer = tf.train.AdamOptimizer(1e-3).minimize(self.total_loss, global_step=self.global_step)
            # new_vars = tf.get_collection(tf.GraphKeys.VARIABLES, scope="optimizer")
            # self.sess.run(tf.variables_initializer(new_vars))

            # tensorflow keeps complaining so fuck it
            self.sess.run(tf.global_variables_initializer(), feed_dict={self.bn: True})

        if ckpt and ckpt.model_checkpoint_path:
            init_model()
            #tf.train.import_meta_graph('%s.meta' % ckpt.model_checkpoint_path)
            self.saver = tf.train.Saver()
            self.saver.restore(self.sess, ckpt.model_checkpoint_path)
            print("restored %s" % ckpt.model_checkpoint_path)
        else:
            init_model()
            self.saver = tf.train.Saver()

        print("SSD model initialized.")

    def single_image(self, sample, min_conf=0.01, nms=0.45):
        resized_img = skimage.transform.resize(sample, (c.image_size, c.image_size))
        pred_labels_f, pred_locs_f, step = self.sess.run([self.pred_labels, self.pred_locs, self.global_step],
                                                         feed_dict={self.imgs_ph: [resized_img], self.bn: False})
        boxes_, confidences_ = matcher.format_output(pred_labels_f[0], pred_locs_f[0])
        loaderutil.resize_boxes(resized_img, sample, boxes_, scale=float(c.image_size))

        return postprocess_boxes(boxes_, confidences_, min_conf, nms)

def model(sess):
    images = tf.placeholder("float", [None, c.image_size, c.image_size, 3])
    bn = tf.placeholder(tf.bool)

    vgg = vgg16.Vgg16()
    with tf.name_scope("content_vgg"):
        vgg.build(images)

    h = [512, 1024, 1024,
         256, 512,
         128, 256,
         128, 256]

    with tf.variable_scope("ssd_extension"):
        #resized = tf.image.resize_images(images, [vgg.conv5_3.get_shape().as_list()[1], vgg.conv5_3.get_shape().as_list()[2]])tf.concat(3, [vgg.conv5_3, resized])
        c6 = tfc.conv2d("c6", vgg.conv5_3, h[0], h[1], bn, size=3)
        c7 = tfc.conv2d("c7", c6, h[1], h[2], bn, size=1)

        c8_1 = tfc.conv2d("c8_1", c7, h[2], h[3], bn, size=1)
        c8_2 = tfc.conv2d("c8_2", c8_1, h[3], h[4], bn, size=3, stride=2)

        c9_1 = tfc.conv2d("c9_1", c8_2, h[4], h[5], bn, size=1)
        c9_2 = tfc.conv2d("c9_2", c9_1, h[5], h[6], bn, size=3, stride=2)

        c10_1 = tfc.conv2d("c10_1", c9_2, h[6], h[7], bn, size=1)
        c10_2 = tfc.conv2d("c10_2", c10_1, h[7], h[8], bn, size=3, stride=2)

        p11 = tf.nn.avg_pool(c10_2, [1, 3, 3, 1], [1, 1, 1, 1], "VALID")

        c_ = 101#c.classes+1

        print("model output classes: %i" % c_)

        topdown = True
        conv43 = vgg.conv4_3

        if topdown:
            #uts = [out1, out2, out3, out4, out5, out6]

            bs = FLAGS.batch_size
            simple = True

            if simple:
                s = c10_2.get_shape().as_list()
                dout5 = tfc.deconv2d("dc5", p11, h[8], h[8], bn, tf.pack([bs, s[1], s[2], h[8]]), size=3, pad="VALID")
                #dout5 = tfc.conv2d("dc5c", dout5, h[8], s[3], bn, size=1)
                c10_2 = tf.concat(3, [c10_2, dout5])

                s = c9_2.get_shape().as_list()
                dout4 = tfc.deconv2d("dc4", c10_2, c10_2.get_shape().as_list()[-1], h[7], bn, tf.pack([bs, s[1], s[2], h[7]]), stride=2)
                #dout4 = tfc.conv2d("dc4c", dout4, h[7], s[3], bn, size=1)
                c9_2 = tf.concat(3, [c9_2, dout4])

                s = c8_2.get_shape().as_list()
                dout3 = tfc.deconv2d("dc3", c9_2, c9_2.get_shape().as_list()[-1], h[5], bn, tf.pack([bs, s[1], s[2], h[5]]), stride=2)
                #dout3 = tfc.conv2d("dc3c", dout3, h[5], s[3], bn, size=1)
                c8_2 = tf.concat(3, [c8_2, dout3])

                s = c7.get_shape().as_list()
                dout2 = tfc.deconv2d("dc2", c8_2, c8_2.get_shape().as_list()[-1], h[3], bn, tf.pack([bs, s[1], s[2], h[3]]), stride=2)
                #dout2 = tfc.conv2d("dc2c", dout2, h[3], s[3], bn, size=1)
                c7 = tf.concat(3, [c7, dout2])

                s = vgg.conv4_3.get_shape().as_list()
                dout1 = tfc.deconv2d("dc1", c7, c7.get_shape().as_list()[-1], h[1], bn, tf.pack([bs, s[1], s[2], h[1]]), stride=2)
                #dout1 = tfc.conv2d("dc1c", dout1, h[1], 512, bn, size=1)
                conv43 = tf.concat(3, [conv43, dout1])
            else:
                s = c10_2.get_shape().as_list()
                dout5 = tfc.deconv2d("dc5", p11, h[8], h[8], bn, tf.pack([bs, s[1], s[2], h[8]]), size=3, pad="VALID")
                dout5 = tfc.conv2d("dc5c", dout5, h[8], s[3], bn, size=1)
                c10_2 = tf.concat(3, [c10_2, dout5])

                s = c9_2.get_shape().as_list()
                dout4 = tfc.deconv2d("dc4", c10_2, c10_2.get_shape().as_list()[-1], h[6], bn,
                                     tf.pack([bs, s[1], s[2], h[6]]), stride=2)
                dout4 = tfc.conv2d("dc4c", dout4, h[6], s[3], bn, size=1)
                c9_2 = tf.concat(3, [c9_2, dout4])

                s = c8_2.get_shape().as_list()
                dout3 = tfc.deconv2d("dc3", c9_2, c9_2.get_shape().as_list()[-1], h[4], bn,
                                     tf.pack([bs, s[1], s[2], h[4]]), stride=2)
                dout3 = tfc.conv2d("dc3c", dout3, h[4], s[3], bn, size=1)
                c8_2 = tf.concat(3, [c8_2, dout3])

                s = c7.get_shape().as_list()
                dout2 = tfc.deconv2d("dc2", c8_2, c8_2.get_shape().as_list()[-1], h[2], bn,
                                     tf.pack([bs, s[1], s[2], h[2]]), stride=2)
                dout2 = tfc.conv2d("dc2c", dout2, h[2], s[3], bn, size=1)
                c7 = tf.concat(3, [c7, dout2])

                s = vgg.conv4_3.get_shape().as_list()
                dout1 = tfc.deconv2d("dc1", c7, c7.get_shape().as_list()[-1], h[0], bn, tf.pack([bs, s[1], s[2], h[0]]),
                                     stride=2)
                dout1 = tfc.conv2d("dc1c", dout1, h[0], 512, bn, size=1)
                conv43 = tf.concat(3, [conv43, dout1])

            #dout1 = tfc.conv2d("dout1", dout1, 512, layer_boxes[0] * (c_ + 4), bn, size=3, act=None)
            #dout2 = tfc.conv2d("dout2", dout2, h[2], layer_boxes[1] * (c_ + 4), bn, size=3, act=None)
            #dout3 = tfc.conv2d("dout3", dout3, h[4], layer_boxes[2] * (c_ + 4), bn, size=3, act=None)
            #dout4 = tfc.conv2d("dout4", dout4, h[6], layer_boxes[3] * (c_ + 4), bn, size=3, act=None)
            #dout5 = tfc.conv2d("dout5", dout5, h[8], layer_boxes[4] * (c_ + 4), bn, size=3, act=None)

            #out1 += dout1
            #out2 += dout2
            #out3 += dout3
            #out4 += dout4
            #out5 += dout5
            #out5 += dout5

        out1 = tfc.conv2d("out1", conv43, conv43.get_shape().as_list()[-1], layer_boxes[0] * (c_ + 4), bn, size=3, act=None)
        out2 = tfc.conv2d("out2", c7, c7.get_shape().as_list()[-1], layer_boxes[1] * (c_ + 4), bn, size=3, act=None)
        out3 = tfc.conv2d("out3", c8_2, c8_2.get_shape().as_list()[-1], layer_boxes[2] * (c_ + 4), bn, size=3, act=None)
        out4 = tfc.conv2d("out4", c9_2, c9_2.get_shape().as_list()[-1], layer_boxes[3] * (c_ + 4), bn, size=3, act=None)
        out5 = tfc.conv2d("out5", c10_2, c10_2.get_shape().as_list()[-1], layer_boxes[4] * (c_ + 4), bn, size=3, act=None)
        out6 = tfc.conv2d("out6", p11, p11.get_shape().as_list()[-1], layer_boxes[5] * (c_ + 4), bn, size=1, act=None)

        s = conv43.get_shape().as_list()
        seg_out = tfc.deconv2d("seg_out", conv43, conv43.get_shape().as_list()[-1], c_, bn, tf.pack([FLAGS.batch_size, s[1]*2, s[2]*2, c_]), stride=2)

    #new_vars = tf.get_collection(tf.GraphKeys.VARIABLES, scope="ssd_extension")
    #sess.run(tf.variables_initializer(new_vars))
    outputs = [out1, out2, out3, out4, out5, out6]

    outfs = []
    for i, out in zip(range(len(outputs)), outputs):
        w = out.get_shape().as_list()[2]
        h = out.get_shape().as_list()[1]
        outf = tf.reshape(out, [-1, w*h*layer_boxes[i], c_ + 4])
        outfs.append(outf)

    formatted_outs = tf.concat(1, outfs) # all (~20000 for MS COCO settings) boxes are now lined up for each image

    pred_labels = formatted_outs[:, :, :c_]

    label_prob = tf.nn.softmax(pred_labels)
    pred_labels_amax = tf.reduce_max(label_prob, reduction_indices=2)
    pred_labels_arg = tf.argmax(pred_labels, axis=2)

    pred_locs = formatted_outs[:, :, c_:]

    return images, bn, outputs, pred_labels, pred_locs, pred_labels_amax, pred_labels_arg, seg_out

def smooth_l1(x):
    l2 = 0.5 * (x**2.0)
    l1 = tf.abs(x) - 0.5

    condition = tf.less(tf.abs(x), 1.0)
    re = tf.select(condition, l2, l1)

    return re

def loss(pred_labels, pred_locs, pred_seg, total_boxes):
    positives = tf.placeholder(tf.float32, [None, total_boxes])
    negatives = tf.placeholder(tf.float32, [None, total_boxes])
    true_labels = tf.placeholder(tf.int32, [None, total_boxes])
    true_locs = tf.placeholder(tf.float32, [None, total_boxes, 4])
    true_seg = tf.placeholder(tf.int32, [None, 76, 76])

    posandnegs = positives + negatives

    class_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(pred_labels, true_labels) * posandnegs
    class_loss = tf.reduce_sum(class_loss, reduction_indices=1) / (1e-5 + tf.reduce_sum(posandnegs, reduction_indices=1))
    loc_loss = tf.reduce_sum(smooth_l1(pred_locs - true_locs), reduction_indices=2) * positives
    loc_loss = tf.reduce_sum(loc_loss, reduction_indices=1) / (1e-5 + tf.reduce_sum(positives, reduction_indices=1))

    seg_loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(pred_seg, true_seg))

    total_loss = tf.reduce_mean(class_loss + 1.0 * loc_loss + seg_loss)

    return positives, negatives, true_labels, true_locs, true_seg, total_loss, tf.reduce_mean(class_loss), tf.reduce_mean(loc_loss), tf.reduce_mean(seg_loss)

def box_scale(k):
    s_min = c.box_s_min
    s_max = 0.95
    m = 6.0

    s_k = s_min + (s_max - s_min) * (k - 1.0) / (m - 1.0) # equation 2

    return s_k

def default_boxes(out_shapes):
    boxes = []

    for o_i in range(len(out_shapes)):
        layer_boxes = []

        layer_shape = out_shapes[o_i]
        s_k = box_scale(o_i + 1)
        s_k1 = box_scale(o_i + 2)

        for x in range(layer_shape[1]):
            x_boxes = []
            for y in range(layer_shape[2]):
                y_boxes = []
                conv4_3 = o_i == 0

                rs = c.box_ratios

                if conv4_3:
                    rs = c.conv4_3_ratios

                for i in range(len(rs)):
                    if conv4_3:
                        scale = c.conv4_3_box_scale
                    else:
                        scale = s_k

                        if i == 0:
                            scale = np.sqrt(s_k * s_k1)

                    default_w = scale * np.sqrt(rs[i])
                    default_h = scale / np.sqrt(rs[i])

                    c_x = (x + 0.5) / float(layer_shape[1])
                    c_y = (y + 0.5) / float(layer_shape[2])

                    y_boxes.append([c_x, c_y, default_w, default_h])
                x_boxes.append(y_boxes)
            layer_boxes.append(x_boxes)
        boxes.append(layer_boxes)

    return boxes

if __name__ == "__main__":
    SSD("summaries/modeltest")