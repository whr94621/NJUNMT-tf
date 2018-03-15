# Copyright 2017 Natural Language Processing Group, Nanjing University, zhaocq.nlp@gmail.com.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Common inference and evaluation functions. """
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy
import re
import tensorflow as tf
from tensorflow import gfile

from njunmt.inference.attention import process_attention_output
from njunmt.inference.attention import pack_batch_attention_dict
from njunmt.inference.attention import dump_attentions
from njunmt.utils.constants import Constants
from njunmt.utils.misc import padding_batch_data
from njunmt.tools.tokenizeChinese import to_chinese_char


def _evaluate(
        sess,
        feed_dict,
        eval_op):
    """ Evaluates one batch of feeding data.

    Args:
        sess: `tf.Session`.
        feed_dict: A dictionary of feeding data.
        eval_op: Tensorflow operation.

    Returns: Results of `eval_op`.

    """
    return sess.run(eval_op, feed_dict=feed_dict)


def evaluate_sentences(
        sources,
        targets,
        sess,
        input_fields,
        eval_op,
        vocab_source,
        vocab_target,
        n_words_src=-1,
        n_words_trg=-1):
    """ Evaluates a list of sentences.

    Args:
        sources: A list of strings.
        targets: A list of strings.
        sess: `tf.Session`.
        input_fields: The dictionary of placeholders.
        eval_op: Tensorflow operation.
        vocab_source: A `Vocab` instance for source side feature map.
        vocab_target: A `Vocab` instance for target side feature map.
        n_words_src: An integer number. If provided and > 0, source side
          token id that exceed this value will be mapped into UNK id.
        n_words_trg: An integer number. If provided and > 0, target side
          token id that exceed this value will be mapped into UNK id.

    Returns: Results of `eval_op`.
    """
    sources = [vocab_source.convert_to_idlist(
        re.split(r"\s*", snt.strip()), n_words_src) for snt in sources]
    targets = [vocab_target.convert_to_idlist(
        re.split(r"\s*", snt.strip()), n_words_trg) for snt in targets]
    ph_x = input_fields[Constants.FEATURE_IDS_NAME]
    ph_x_len = input_fields[Constants.FEATURE_LENGTH_NAME]
    ph_y = input_fields[Constants.LABEL_IDS_NAME]
    ph_y_len = input_fields[Constants.LABEL_LENGTH_NAME]
    x, len_x = padding_batch_data(sources, vocab_source.pad_id)
    y, len_y = padding_batch_data(targets, vocab_target.pad_id)
    feed_dict = {ph_x: x, ph_x_len: len_x,
                 ph_y: y, ph_y_len: len_y}
    return _evaluate(sess, feed_dict, eval_op)


def evaluate(sess, eval_op, eval_data):
    """ Evaluates data by loss.

    Args:
        sess: `tf.Session`.
        eval_op: Tensorflow operation, computing the loss.
        eval_data: An iterable instance that each element
          is a packed feeding dictionary for `sess`.

    Returns: Total loss averaged by number of data samples.
    """
    losses = 0.
    total_size = 0
    for data in eval_data:
        n_samples = len(data["feature_ids"])
        loss = _evaluate(sess, data["feed_dict"], eval_op)
        losses += loss * float(n_samples)
        total_size += n_samples
    loss = losses / float(total_size)
    return loss


def evaluate_with_attention(
        sess,
        eval_op,
        eval_data,
        vocab_source,
        vocab_target,
        attention_op=None,
        output_filename_prefix=None):
    """ Evaluates data by loss.

    Args:
        sess: `tf.Session`.
        eval_op: Tensorflow operation, computing the loss.
        eval_data: An iterable instance that each element
          is a packed feeding dictionary for `sess`.
        vocab_source: A `Vocab` instance for source side feature map.
        vocab_target: A `Vocab` instance for target side feature map.
        attention_op: Tensorflow operation for output attention.
        output_filename_prefix: A string.

    Returns: Total loss averaged by number of data samples.
    """
    losses = 0.
    total_size = 0
    attentions = {}
    for data in eval_data:
        if attention_op is None:
            loss = _evaluate(sess, data["feed_dict"], eval_op)
        else:
            loss, atts = _evaluate(sess, data["feed_dict"], [eval_op, attention_op])
            ss_strs = [vocab_source.convert_to_wordlist(ss, bpe_decoding=False)
                       for ss in data["feature_ids"]]
            tt_strs = [vocab_target.convert_to_wordlist(
                tt, bpe_decoding=False, reverse_seq=False)
                       for tt in data["label_ids"]]
            attentions.update(pack_batch_attention_dict(
                total_size, ss_strs, tt_strs, atts))
        losses += loss * float(len(ss_strs))
        total_size += len(ss_strs)
    loss = losses / float(total_size)
    if attention_op is not None:
        dump_attentions(output_filename_prefix, attentions)
    return loss


def _infer(
        sess,
        feed_dict,
        prediction_op,
        batch_size,
        top_k=1,
        output_attention=False):
    """ Infers a batch of samples with beam search.

    Args:
        sess: `tf.Session`
        feed_dict: A dictionary of feeding data.
        prediction_op: Tensorflow operation for inference.
        batch_size: The batch size.
        top_k: An integer, number of predicted sequences will be
          returned.
        output_attention: Whether to output attention.

    Returns: A tuple `(predicted_sequences, attention_scores)`.
      The `predicted_sequences` is an ndarray of shape
      [`top_k` * `batch_sze`, max_sequence_length].
      The `attention_scores` is None if there is no attention
      related information in `prediction_op`.
    """
    brief_pred_op = dict()
    brief_pred_op["hypothesis"] = prediction_op["sorted_hypothesis"]
    if output_attention:
        brief_pred_op["sorted_argidx"] = prediction_op["sorted_argidx"]
        brief_pred_op["attentions"] = prediction_op["attentions"]
        brief_pred_op["beam_ids"] = prediction_op["beam_ids"]

    predict_out = sess.run(brief_pred_op, feed_dict=feed_dict)
    num_samples = predict_out["hypothesis"].shape[0]
    beam_size = num_samples // batch_size
    # [batch_, beam_]
    batch_beam_pos = numpy.tile(numpy.arange(batch_size) * beam_size, [beam_size, 1]).transpose()
    batch_beam_pos = numpy.reshape(batch_beam_pos[:, :top_k], -1)
    if output_attention:
        argidx = predict_out.pop("sorted_argidx")[batch_beam_pos]
        attentions = process_attention_output(predict_out, argidx)
        return predict_out["hypothesis"][batch_beam_pos, :], attentions
    return predict_out["hypothesis"][batch_beam_pos, :], None


def infer_sentences(
        sources,
        sess,
        input_fields,
        prediction_op,
        vocab_source,
        top_k=1,
        n_words_src=-1):
    """ Infers a list of sentences.

    Args:
        sources: A list of strings.
        sess: `tf.Session`.
        input_fields: The dictionary of placeholders.
        prediction_op: Tensorflow operation for inference.
        vocab_source: A `Vocab` instance for source side feature map.
        alpha: A scalar number, length penalty rate. If not provided
          or < 0, simply average each beam by length of predicted
          sequence.
        top_k: An integer, number of predicted sequences will be
          returned.
        n_words_src: An integer number. If provided and > 0, source side
          token id that exceed this value will be mapped into UNK id.

    Returns: A tuple `(predicted_sequences, attention_scores)`.
      The `predicted_sequences` is an ndarray of shape
      [`top_k`, max_sequence_length].
      The `attention_scores` is None if there is no attention
      related information in `prediction_op`.
    """
    sources = [vocab_source.convert_to_idlist(
        re.split(r"\s*", snt.strip()), n_words_src) for snt in sources]
    ph_x = input_fields[Constants.FEATURE_IDS_NAME]
    ph_x_len = input_fields[Constants.FEATURE_LENGTH_NAME]
    x, len_x = padding_batch_data(sources, vocab_source.pad_id)
    feed_dict = {ph_x: x, ph_x_len: len_x}
    return _infer(sess, feed_dict, prediction_op, len(x), top_k)


def infer(
        sess,
        prediction_op,
        infer_data,
        output,
        vocab_source,
        vocab_target,
        delimiter=" ",
        output_attention=False,
        tokenize_output=False,
        verbose=True):
    """ Infers data and save the prediction results.

    Args:
        sess: `tf.Session`.
        prediction_op: Tensorflow operation for inference.
        infer_data: An iterable instance that each element
          is a packed feeding dictionary for `sess`.
        output: Output file name, `str`.
        vocab_source: A `Vocab` instance for source side feature map.
        vocab_target: A `Vocab` instance for target side feature map.
        alpha: A scalar number, length penalty rate. If not provided
          or < 0, simply average each beam by length of predicted
          sequence.
        delimiter: The delimiter of output token sequence.
        output_attention: Whether to output attention information.
        tokenize_output: Whether to split words into characters
          (only for Chinese).
        verbose: Print inference information if set True.

    Returns: A tuple `(sources, hypothesis)`, two lists of
      strings.
    """
    attentions = dict()
    hypothesis = []
    sources = []
    cnt = 0
    for data in infer_data:
        source_tokens = [vocab_source.convert_to_wordlist(x, bpe_decoding=False)
                         for x in data["feature_ids"]]
        x_str = [delimiter.join(x) for x in source_tokens]
        prediction, att = _infer(sess, data["feed_dict"], prediction_op,
                                 len(x_str), top_k=1, output_attention=output_attention)

        sources.extend(x_str)
        hypothesis.extend([delimiter.join(vocab_target.convert_to_wordlist(prediction[sample_idx]))
                           for sample_idx in range(prediction.shape[0])])
        if output_attention and att is not None:
            candidate_tokens = [vocab_target.convert_to_wordlist(
                prediction[idx, :], bpe_decoding=False, reverse_seq=False)
                                for idx in range(len(x_str))]

            attentions.update(pack_batch_attention_dict(
                cnt, source_tokens, candidate_tokens, att))
        cnt += len(x_str)
        if verbose:
            tf.logging.info(cnt)
    if tokenize_output:
        hypothesis = to_chinese_char(hypothesis)
    if output:
        with gfile.GFile(output, "w") as fw:
            fw.write("\n".join(hypothesis) + "\n")
    if output_attention:
        dump_attentions(output, attentions)
    return sources, hypothesis
