import json
from time import time
from os import path
from multiprocessing.pool import ThreadPool
from collections import defaultdict

from kerastuner import config
from .tunercallback import TunerCallback
from kerastuner.collections import MetricsCollection
from kerastuner.abstractions.display import write_log, fatal, info, section
from kerastuner.abstractions.io import save_model, write_file


class MonitorCallback(TunerCallback):

    def __init__(self, tuner_state, instance_state, execution_state,
                 cloudservice, refresh_interval=2, num_threads=4):
        super(MonitorCallback, self).__init__(tuner_state, instance_state,
                                              execution_state, cloudservice)
        self.last_refresh = -1
        self.refresh_interval = refresh_interval
        self.thread_pool = ThreadPool(num_threads)
        self.epoch_history = defaultdict(list)
        self.training_complete = False  # important for the cloudservice

    def on_batch_end(self, batch, logs={}):
        for metric, value in logs.items():
            self.epoch_history[metric].append(float(value))
        self._report_status()

    def on_epoch_end(self, epoch, logs={}):

        # update epoch counters
        self.execution_state.epochs += 1
        self.tuner_state.remaining_budget -= 1

        # update metrics and checkpoint if needed
        for metric, value in logs.items():
            improved = self.execution_state.metrics.update(metric, value)
            if self.tuner_state.objective == metric and improved:
                self.thread_pool.apply_async(self._checkpoint_model)

        # reset epoch history
        self.epoch_history = defaultdict(list)

        # update status
        self._report_status(force=True)

    def on_train_begin(self, logs={}):
        # FIXME: move to display tuner
        section("Training execution %s/%s" %
                (self.instance_state.execution_trained + 1,
                 self.tuner_state.num_executions))

    def on_train_end(self, logs={}):
        self.training_complete = True
        self._end_training_statistics()
        self._report_status(force=True)
        self._write_result_file()

        if self.tuner_state.remaining_budget < 1:
            self._tuning_complete()

    def _end_training_statistics(self):
        """Compute and display end of training statistics

        Notes:
            update order matters must be instance then global
        """

        # update instance aggregate statistics by reporting the best value
        for metric in self.execution_state.metrics.to_list():
            self.instance_state.agg_metrics.update(metric.name, metric.get_best_value())  # nopep8

        # update tuner overall objective metric
        update_best_model = False
        for metric in self.instance_state.agg_metrics.to_list():
            improved = self.tuner_state.agg_metrics.update(metric.name, metric.get_best_value())  # nopep8
            if metric.name == self.tuner_state.objective and improved:
                update_best_model = True

        # record which one is the best model
        # ! dont try to simplify - must be after all statistics are computed
        if update_best_model or not self.tuner_state.best_instance_config:
            config = self.instance_state.to_config()
            self.tuner_state.best_instance_config = config

        # record execution config in instance
        self.instance_state.execution_configs.append(self.execution_state.to_config())  # nopep8

    def _checkpoint_model(self):
        """Checkpoint model"""
        prefix = self._get_filename_prefix()
        base_filename = path.join(self.tuner_state.host.local_dir, prefix)
        save_model(self.model, base_filename, output_type="keras")
        write_log("Improved model saved to %s" % base_filename)
        self._write_result_file()

    def _write_result_file(self):
        """Record results - one file per instance"""
        status = {
            "update_time": int(time()),
            "tuner": self.tuner_state.to_config(),
            "instance": self.instance_state.to_config(),
            "hparams": config._DISTRIBUTIONS.get_hyperparameters_config()
        }
        status_json = json.dumps(status)
        prefix = self._get_filename_prefix(with_execution_info=False)
        # don't do a os.join as it is just appending a suffix
        fname = prefix + '-results.json'
        write_file(fname, status_json)

        # send result to the cloud service
        if self.cloudservice.is_enable:
            self.cloudservice.send_results(status)

    def _report_status(self, force=False):
        "update the status.json file"
        delta = time() - self.last_refresh
        if delta < self.refresh_interval and not force:
            return
        # FIXME: can we make it async?
        # self.thread_pool.apply_async(self._report_status_worker)
        self._report_status_worker()
        self.last_refresh = time()

    def _tuning_complete(self):
        "Final message when tuning (budget_remaining < 1) is complete"
        info("Hypertuning complete - results in %s" %
             self.tuner_state.host.result_dir)

    def _report_status_worker(self):
        "Report tuner status periodically"
        # getting stats
        status = {
            "update_time": int(time()),
            "tuner": self.tuner_state.to_config(),
            "instance": self.instance_state.to_config(),
            "execution": self.execution_state.to_config(),
            "hparams": config._DISTRIBUTIONS.get_hyperparameters_config()
        }
        # needed for cloudservice
        status['training_complete'] = self.training_complete
        status['epoch_history'] = self.epoch_history
        status_json = json.dumps(status)

        # write on disk
        fname = path.join(self.tuner_state.host.result_dir, 'status.json')
        write_file(fname, status_json)

        # send status to cloudservice
        if self.cloudservice.is_enable:
            self.cloudservice.send_status(status)

    def _get_filename_prefix(self, with_execution_info=True):
        "Build dir/filename prefix based of the instance and execution trained"
        prefix = '%s-%s-%s' % (self.tuner_state.project,
                               self.tuner_state.architecture,
                               self.instance_state.idx)
        if with_execution_info:
            prefix += '-%s' % self.execution_state.idx
        return path.join(self.tuner_state.host.result_dir, prefix)