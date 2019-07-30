# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
r"""Converts a trained checkpoint into a frozen model for mobile inference.

Once you've trained a model using the `train.py` script, you can use this tool
to convert it into a binary GraphDef file that can be loaded into the Android,
iOS, or Raspberry Pi example code. Here's an example of how to run it:

bazel run tensorflow/examples/speech_commands/freeze -- \
--sample_rate=16000 --dct_coefficient_count=40 --window_size_ms=20 \
--window_stride_ms=10 --clip_duration_ms=1000 \
--model_architecture=conv \
--start_checkpoint=/tmp/speech_commands_train/conv.ckpt-1300 \
--output_file=/tmp/my_frozen_graph.pb

One thing to watch out for is that you need to pass in the same arguments for
`sample_rate` and other command line variables here as you did for the training
script.

The resulting graph has an input for WAV-encoded data named 'wav_data', one for
raw PCM data (as floats in the range -1.0 to 1.0) called 'decoded_sample_data',
and the output is called 'labels_softmax'.

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os.path
import sys
import os
import glob
import tensorflow as tf
from speechclas import paths,config
from datetime import datetime

from tensorflow.contrib.framework.python.ops import audio_ops as contrib_audio
from speechclas import input_data, models, paths
from tensorflow.python.framework import graph_util

CONF = config.conf_dict()
timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')

def create_inference_graph(wanted_words, sample_rate, clip_duration_ms,
                           clip_stride_ms, window_size_ms, window_stride_ms,
                           dct_coefficient_count, model_architecture):
  
  """Creates an audio model with the nodes needed for inference.

  Uses the supplied arguments to create a model, and inserts the input and
  output nodes that are needed to use the graph for inference.

  Args:
    wanted_words: Comma-separated list of the words we're trying to recognize.
    sample_rate: How many samples per second are in the input audio files.
    clip_duration_ms: How many samples to analyze for the audio pattern.
    clip_stride_ms: How often to run recognition. Useful for models with cache.
    window_size_ms: Time slice duration to estimate frequencies from.
    window_stride_ms: How far apart time slices should be.
    dct_coefficient_count: Number of frequency bands to analyze.
    model_architecture: Name of the kind of model to generate.
  """

  words_list = input_data.prepare_words_list(wanted_words.split(','))
  model_settings = models.prepare_model_settings(
      len(words_list), sample_rate, clip_duration_ms, window_size_ms,
      window_stride_ms, dct_coefficient_count)
  runtime_settings = {'clip_stride_ms': clip_stride_ms}

  wav_data_placeholder = tf.placeholder(tf.string, [], name='wav_data')
  decoded_sample_data = contrib_audio.decode_wav(
      wav_data_placeholder,
      desired_channels=1,
      desired_samples=model_settings['desired_samples'],
      name='decoded_sample_data')
  spectrogram = contrib_audio.audio_spectrogram(
      decoded_sample_data.audio,
      window_size=model_settings['window_size_samples'],
      stride=model_settings['window_stride_samples'],
      magnitude_squared=True)
  fingerprint_input = contrib_audio.mfcc(
      spectrogram,
      decoded_sample_data.sample_rate,
      dct_coefficient_count=dct_coefficient_count)
  fingerprint_frequency_size = model_settings['dct_coefficient_count']
  fingerprint_time_size = model_settings['spectrogram_length']
  reshaped_input = tf.reshape(fingerprint_input, [
      -1, fingerprint_time_size * fingerprint_frequency_size
  ])

  logits = models.create_model(
      reshaped_input, model_settings, model_architecture, is_training=False,
      runtime_settings=runtime_settings)

  # Create an output to use for inference.
  tf.nn.softmax(logits, name='labels_softmax')


def generatepb(TIMESTAMP,CONF):
  sess = tf.InteractiveSession()
  # Create the model and load its weights.
  create_inference_graph(CONF['model_settings']['wanted_words'], CONF['model_settings']['sample_rate'],
                         CONF['model_settings']['clip_duration_ms'], CONF['audio_processor']['clip_stride_ms'],
                         CONF['model_settings']['window_size_ms'], CONF['model_settings']['window_stride_ms'],
                         CONF['model_settings']['dct_coefficient_count'], CONF['training_parameters']['model_architecture'])
  
  
  
  lastckpt=max(glob.glob(paths.get_timestamped_dir()+ '/ckpts/*'), key=os.path.getmtime)
  lastmodel = lastckpt.split(".meta", 1)[0]
  models.load_variables_from_checkpoint(sess, lastmodel)
  
  # Turn all the variables into inline constants inside the graph and save it.
  frozen_graph_def = graph_util.convert_variables_to_constants(
      sess, sess.graph_def, ['labels_softmax'])

  tf.train.write_graph(
      frozen_graph_def,
      paths.get_checkpoints_dir(),
      os.path.basename(CONF['training_parameters']['output_file']),
      as_text=False)
  tf.logging.info('Saved frozen graph to %s', CONF['training_parameters']['output_file'])
  sess.close()
