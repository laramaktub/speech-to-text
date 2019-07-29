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
r"""Simple speech recognition to spot a limited number of keywords.

This is a self-contained example script that will train a very basic audio
recognition model in TensorFlow. It downloads the necessary training data and
runs with reasonable defaults to train within a few hours even only using a CPU.
For more information, please see
https://www.tensorflow.org/tutorials/audio_recognition.

It is intended as an introduction to using neural networks for audio
recognition, and is not a full speech recognition system. For more advanced
speech systems, I recommend looking into Kaldi. This network uses a keyword
detection style to spot discrete words from a small vocabulary, consisting of
"yes", "no", "up", "down", "left", "right", "on", "off", "stop", and "go".

To run the training process, use:

bazel run tensorflow/examples/speech_commands:train

This will write out checkpoints to /tmp/speech_commands_train/, and will
download over 1GB of open source training data, so you'll need enough free space
and a good internet connection. The default data is a collection of thousands of
one-second .wav files, each containing one spoken word. This data set is
collected from https://aiyprojects.withgoogle.com/open_speech_recording, please
consider contributing to help improve this and other models!

As training progresses, it will print out its accuracy metrics, which shouldrise above 90% by the end. Once it's complete, you can run the freeze script to
get a binary GraphDef that you can easily deploy on mobile applications.

If you want to train on your own data, you'll need to create .wavs with your
recordings, all at a consistent length, and then arrange them into subfolders
organized by label. For example, here's a possible file structure:

my_wavs >
  up >
    audio_0.wav
    audio_1.wav
  down >
    audio_2.wav
    audio_3.wav
  other>
    audio_4.wav
    audio_5.wav

You'll also need to tell the script what labels to look for, using the
`--wanted_words` argument. In this case, 'up,down' might be what you want, and
the audio in the 'other' folder would be used to train an 'unknown' category.

To pull this all together, you'd run:

bazel run tensorflow/examples/speech_commands:train -- \
--data_dir=my_wavs --wanted_words=up,down

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os.path
import sys
import time

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
import logging
from tensorflow.contrib.framework.python.ops import audio_ops as contrib_audio
from datetime import datetime
from tensorflow.python.platform import gfile
from speechclas import paths, config, input_data, models, freeze, utils, model_utils
from tensorflow.python.framework import graph_util

CONF = config.conf_dict()
timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')


def train_fn(TIMESTAMP, CONF):
  
  sess = tf.InteractiveSession()
  paths.timestamp = TIMESTAMP
  paths.CONF = CONF
  print(CONF)

  utils.create_dir_tree()
  #Activate only if you want to make a backup of the splits used for the training
  #utils.backup_splits()
  
#  logging.set_verbosity(logging.INFO)
  logging.basicConfig(filename=paths.get_logs_dir()+'/train_info.log',level=logging.DEBUG)

  # Begin by making sure we have the training data we need. If you already have
  # training data of your own, use `--data_url= ` on the command line to avoid
  # downloading.
  model_settings = models.prepare_model_settings(
      len(input_data.prepare_words_list(CONF["model_settings"]["wanted_words"].split(','))),
      CONF["model_settings"]["sample_rate"], CONF["model_settings"]["clip_duration_ms"], CONF["model_settings"]["window_size_ms"],
      CONF["model_settings"]["window_stride_ms"], CONF['model_settings']['feature_bin_count'])
  audio_processor = input_data.AudioProcessor(
      paths.get_audio_url(), paths.get_audio_dir(), CONF["audio_processor"]["silence_percentage"],
      CONF["audio_processor"]["unknown_percentage"],
      CONF["model_settings"]["wanted_words"].split(','), CONF["training_parameters"]["validation_percentage"],
      CONF["training_parameters"]["testing_percentage"], model_settings)
  fingerprint_size = model_settings['fingerprint_size']
  label_count = model_settings['label_count']
  time_shift_samples = int((CONF["audio_processor"]["time_shift_ms"] * CONF["audio_processor"]["sample_rate"]) / 1000)
  # Figure out the learning rates for each training phase. Since it's often
  # effective to have high learning rates at the start of training, followed by
  # lower levels towards the end, the number of steps and learning rates can be
  # specified as comma-separated lists to define the rate at each stage. For
  # example --how_many_training_steps=10000,3000 --learning_rate=0.001,0.0001
  # will run 13,000 training loops in total, with a rate of 0.001 for the first
  # 10,000, and 0.0001 for the final 3,000.
  training_steps_list = list(map(int, CONF['training_parameters']['how_many_training_steps'].split(',')))
  learning_rates_list = list(map(float, CONF['training_parameters']['learning_rate'].split(',')))
  if len(training_steps_list) != len(learning_rates_list):
    raise Exception(
        '--how_many_training_steps and --learning_rate must be equal length '
        'lists, but are %d and %d long instead' % (len(training_steps_list),
                                                   len(learning_rates_list)))

  fingerprint_input = tf.placeholder(
      tf.float32, [None, fingerprint_size], name='fingerprint_input')

  logits, dropout_prob = models.create_model(
      fingerprint_input,
      model_settings,
      CONF['training_parameters']['model_architecture'],
      is_training=True)

  # Define loss and optimizer
  ground_truth_input = tf.placeholder(
      tf.int64, [None], name='groundtruth_input')

  # Optionally we can add runtime checks to spot when NaNs or other symptoms of
  # numerical errors start occurring during training.
  control_dependencies = []
  if CONF['training_parameters']['check_nans']:
    checks = tf.add_check_numerics_ops()
    control_dependencies = [checks]

  # Create the back propagation and training evaluation machinery in the graph.
  with tf.name_scope('cross_entropy'):
    cross_entropy_mean = tf.losses.sparse_softmax_cross_entropy(
        labels=ground_truth_input, logits=logits)
  tf.summary.scalar('cross_entropy', cross_entropy_mean)
  with tf.name_scope('train'), tf.control_dependencies(control_dependencies):
    learning_rate_input = tf.placeholder(
        tf.float32, [], name='learning_rate_input')
    train_step = tf.train.GradientDescentOptimizer(
        learning_rate_input).minimize(cross_entropy_mean)
  predicted_indices = tf.argmax(logits, 1)
  correct_prediction = tf.equal(predicted_indices, ground_truth_input)
  confusion_matrix = tf.confusion_matrix(
      ground_truth_input, predicted_indices, num_classes=label_count)
  evaluation_step = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))
  tf.summary.scalar('accuracy', evaluation_step)

  global_step = tf.train.get_or_create_global_step()
  increment_global_step = tf.assign(global_step, global_step + 1)

  saver = tf.train.Saver(tf.global_variables())

  # Merge all the summaries and write them out to /tmp/retrain_logs (by default)
  merged_summaries = tf.summary.merge_all()
  train_writer = tf.summary.FileWriter(CONF['training_parameters']['summaries_dir'] + '/train',
                                       sess.graph)
  validation_writer = tf.summary.FileWriter(CONF['training_parameters']['summaries_dir'] + '/validation')

  tf.global_variables_initializer().run()

  start_step = 1

  if CONF['training_parameters']['start_checkpoint']:
    models.load_variables_from_checkpoint(sess, CONF['training_parameters']['start_checkpoint'])
    start_step = global_step.eval(session=sess)

  logging.info('Training from step: %d ', start_step)

  # Save graph.pbtxt.
  tf.train.write_graph(sess.graph_def,paths.get_checkpoints_dir() ,
                       CONF['training_parameters']['model_architecture'] + '.pbtxt')

  # Save list of words.

  with gfile.GFile(
      os.path.join(paths.get_checkpoints_dir(), CONF['training_parameters']['model_architecture'] + '_labels.txt'),
      'w') as f:
    
    f.write('\n'.join(audio_processor.words_list))

  # Training loop.
  training_steps_max = np.sum(training_steps_list)
  for training_step in xrange(start_step, training_steps_max + 1):
    # Figure out what the current learning rate is.
    training_steps_sum = 0
    for i in range(len(training_steps_list)):
      training_steps_sum += training_steps_list[i]
      if training_step <= training_steps_sum:
        learning_rate_value = learning_rates_list[i]
        break
    # Pull the audio samples we'll use for training.
    train_fingerprints, train_ground_truth = audio_processor.get_data(
        CONF['training_parameters']['batch_size'], 0, model_settings, CONF['training_parameters']['background_frequency'],
        CONF['training_parameters']['background_volume'], time_shift_samples, 'training', sess)
    # Run the graph with this batch of training data.
    train_summary, train_accuracy, cross_entropy_value, _, _ = sess.run(
        [
            merged_summaries, evaluation_step, cross_entropy_mean, train_step,
            increment_global_step
        ],
        feed_dict={
            fingerprint_input: train_fingerprints,
            ground_truth_input: train_ground_truth,
            learning_rate_input: learning_rate_value,
            dropout_prob: 0.5
        })
    train_writer.add_summary(train_summary, training_step)
    logging.info('Step #%d: rate %f, accuracy %.1f%%, cross entropy %f' %
                    (training_step, learning_rate_value, train_accuracy * 100,
                     cross_entropy_value))
    is_last_step = (training_step == training_steps_max)
    if (training_step % CONF['training_parameters']['eval_step_interval']) == 0 or is_last_step:
      set_size = audio_processor.set_size('validation')
      total_accuracy = 0
      total_conf_matrix = None
      for i in xrange(0, set_size, CONF['training_parameters']['batch_size']):
        validation_fingerprints, validation_ground_truth = (
            audio_processor.get_data(CONF['training_parameters']['batch_size'], i, model_settings, 0.0,
                                     0.0, 0, 'validation', sess))
        # Run a validation step and capture training summaries for TensorBoard
        # with the `merged` op.
        validation_summary, validation_accuracy, conf_matrix = sess.run(
            [merged_summaries, evaluation_step, confusion_matrix],
            feed_dict={
                fingerprint_input: validation_fingerprints,
                ground_truth_input: validation_ground_truth,
                dropout_prob: 1.0
            })
        validation_writer.add_summary(validation_summary, training_step)
        batch_size = min(CONF['training_parameters']['batch_size'], set_size - i)
        total_accuracy += (validation_accuracy * batch_size) / set_size
        if total_conf_matrix is None:
          total_conf_matrix = conf_matrix
        else:
          total_conf_matrix += conf_matrix
      logging.info('Confusion Matrix:\n %s' % (total_conf_matrix))
      logging.info('Step %d: Validation accuracy = %.1f%% (N=%d)' %
                      (training_step, total_accuracy * 100, set_size))

    # Save the model checkpoint periodically.
    if (training_step % CONF['training_parameters']['save_step_interval'] == 0 or
        training_step == training_steps_max):
      checkpoints_path = os.path.join( paths.get_checkpoints_dir(),
                                     CONF['training_parameters']['model_architecture'] + '.ckpt')
      logging.info('Saving to "%s-%d"', checkpoints_path, training_step)
      saver.save(sess, checkpoints_path, global_step=training_step)
  
  set_size = audio_processor.set_size('testing')
  logging.info('set_size=%d', set_size)
  total_accuracy = 0
  total_conf_matrix = None
  for i in xrange(0, set_size, CONF['training_parameters']['batch_size']):
    test_fingerprints, test_ground_truth = audio_processor.get_data(
        CONF['training_parameters']['batch_size'], i, model_settings, 0.0, 0.0, 0, 'testing', sess)
    test_accuracy, conf_matrix = sess.run(
        [evaluation_step, confusion_matrix],
        feed_dict={
            fingerprint_input: test_fingerprints,
            ground_truth_input: test_ground_truth,
            dropout_prob: 1.0
        })
    batch_size = min(CONF['training_parameters']['batch_size'], set_size - i)
    total_accuracy += (test_accuracy * batch_size) / set_size
    if total_conf_matrix is None:
      total_conf_matrix = conf_matrix
    else:
      total_conf_matrix += conf_matrix
  logging.info('Confusion Matrix:\n %s' % (total_conf_matrix))
  logging.info('Final test accuracy = %.1f%% (N=%d)' % (total_accuracy * 100,
                                                           set_size))
  print('Saving data to {} folder.'.format(paths.get_timestamped_dir()))

  print('Saving the configuration ...')
  model_utils.save_conf(CONF)
  tf.reset_default_graph()
  freeze.generatepb(TIMESTAMP=timestamp, CONF=CONF)




