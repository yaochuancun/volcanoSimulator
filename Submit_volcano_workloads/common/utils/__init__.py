"""Common utilities: YAML loading, retry decorator, time strings, and results directory archival."""

import datetime
import logging
import os
import shutil

import yaml


def load_from_file(filename: str):
    """Read YAML from file (may be multi-document); return list of documents."""
    with open(filename, 'r') as file:
        workload = list(yaml.safe_load_all(file))
        logging.info(f'Workload {filename} loaded, with {len(workload)} jobs')
        return workload


def do_until_no_error(func):
    """Decorator: on exception, log and retry indefinitely until success."""

    def wrapper(*args, **kwargs):
        while True:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logging.exception(e)
    return wrapper


def now_str():
    """Current time as ``YYYY-MM-DD-HH-MM-SS`` for directory names, etc."""
    return datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')


def now_str_millisecond():
    """Current time including microsecond suffix."""
    return datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S.%f')


def makeup_results_dir():
    """If ``results`` already exists, move it to ``old-results/<timestamp>`` and recreate empty ``results``."""
    results_dir_exists = os.path.exists('results')
    os.makedirs('results', exist_ok=True)
    if results_dir_exists:
        os.makedirs('old-results', exist_ok=True)
        shutil.move('results', 'old-results')
        os.rename('old-results/results', f'old-results/{now_str()}')
        os.makedirs('results', exist_ok=True)
