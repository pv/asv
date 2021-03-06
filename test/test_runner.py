# -*- coding: utf-8 -*-
# Licensed under a 3-clause BSD style license - see LICENSE.rst

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import sys
import os
import shutil
import datetime
import pstats
import collections
import socket
import json

import six
import pytest

from os.path import join

from asv import benchmarks
from asv import config
from asv import environment
from asv import runner
from asv import util
from asv.results import Results

from .test_benchmarks import benchmarks_fixture, ASV_CONF_JSON, BENCHMARK_DIR


ON_PYPY = hasattr(sys, 'pypy_version_info')


class ResultsWrapper(object):
    tuple_type = collections.namedtuple('tuple_type', ['result', 'stats', 'samples',
                                                       'params', 'stderr', 'errcode',
                                                       'profile', 'started_at', 'ended_at'])

    def __init__(self, results, benchmarks):
        self.results = results
        self.benchmarks = benchmarks

    def __len__(self):
        return len(list(self.results.get_all_result_keys()))

    def items(self):
        for key in self.results.get_all_result_keys():
            yield key, self[key]

    def __getitem__(self, key):
        params = self.benchmarks[key]['params']
        return self.tuple_type(result=self.results.get_result_value(key, params),
                               stats=self.results.get_result_stats(key, params),
                               samples=self.results.get_result_samples(key, params),
                               stderr=self.results.stderr.get(key),
                               errcode=self.results.errcode.get(key),
                               params=params,
                               profile=(self.results.get_profile(key)
                                        if self.results.has_profile(key) else None),
                               started_at=self.results.started_at[key],
                               ended_at=self.results.ended_at[key])


@pytest.mark.flaky(reruns=1, reruns_delay=5)
def test_run_benchmarks(benchmarks_fixture, tmpdir):
    conf, repo, envs, commit_hash = benchmarks_fixture

    start_timestamp = datetime.datetime.utcnow()

    b = benchmarks.Benchmarks.discover(conf, repo, envs, [commit_hash])

    # Old results to append to
    results = Results.unnamed()
    name = 'time_examples.TimeSuite.time_example_benchmark_1'
    results.add_result(b[name],
                       runner.BenchmarkResult(result=[1],
                                              samples=[[42.0, 24.0]],
                                              number=[1],
                                              errcode=0, stderr='', profile=None),
                       record_samples=True)

    # Run
    runner.run_benchmarks(
        b, envs[0], results=results, profile=True, show_stderr=True,
        append_samples=True, record_samples=True)
    times = ResultsWrapper(results, b)

    end_timestamp = datetime.datetime.utcnow()

    assert len(times) == len(b)
    assert times[
        'time_examples.TimeSuite.time_example_benchmark_1'].result != [None]
    stats = results.get_result_stats(name, b[name]['params'])
    assert isinstance(stats[0]['std'], float)
    # The exact number of samples may vary if the calibration is not fully accurate
    samples = results.get_result_samples(name, b[name]['params'])
    assert len(samples[0]) >= 4
    # Explicitly provided 'prev_samples` should come first
    assert samples[0][:2] == [42.0, 24.0]
    # Benchmarks that raise exceptions should have a time of "None"
    assert times[
        'time_secondary.TimeSecondary.time_exception'].result == [None]
    assert times[
        'subdir.time_subdir.time_foo'].result != [None]
    if not ON_PYPY:
        # XXX: the memory benchmarks don't work on Pypy, since asizeof
        # is CPython-only
        assert times[
            'mem_examples.mem_list'].result[0] > 1000
    assert times[
        'time_secondary.track_value'].result == [42.0]
    assert times['time_secondary.track_value'].profile is not None
    assert isinstance(times['time_examples.time_with_warnings'].stderr, type(''))
    assert times['time_examples.time_with_warnings'].errcode != 0

    assert times['time_examples.TimeWithBadTimer.time_it'].result == [0.0]

    assert times['params_examples.track_param'].params == [["<class 'benchmark.params_examples.ClassOne'>",
                                                               "<class 'benchmark.params_examples.ClassTwo'>"]]
    assert times['params_examples.track_param'].result == [42, 42]

    assert times['params_examples.mem_param'].params == [['10', '20'], ['2', '3']]
    assert len(times['params_examples.mem_param'].result) == 2*2

    assert times['params_examples.ParamSuite.track_value'].params == [["'a'", "'b'", "'c'"]]
    assert times['params_examples.ParamSuite.track_value'].result == [1+0, 2+0, 3+0]

    assert isinstance(times['params_examples.TuningTest.time_it'].result[0], float)
    assert isinstance(times['params_examples.TuningTest.time_it'].result[1], float)

    assert isinstance(times['params_examples.time_skip'].result[0], float)
    assert isinstance(times['params_examples.time_skip'].result[1], float)
    assert util.is_nan(times['params_examples.time_skip'].result[2])

    assert times['peakmem_examples.peakmem_list'].result[0] >= 4 * 2**20

    assert times['cache_examples.ClassLevelSetup.track_example'].result == [500]
    assert times['cache_examples.ClassLevelSetup.track_example2'].result == [500]

    assert times['cache_examples.track_cache_foo'].result == [42]
    assert times['cache_examples.track_cache_bar'].result == [12]
    assert times['cache_examples.track_my_cache_foo'].result == [0]

    assert times['cache_examples.ClassLevelSetupFail.track_fail'].result == [None]
    assert 'raise RuntimeError()' in times['cache_examples.ClassLevelSetupFail.track_fail'].stderr

    assert times['cache_examples.ClassLevelCacheTimeout.track_fail'].result == [None]
    assert times['cache_examples.ClassLevelCacheTimeoutSuccess.track_success'].result == [0]

    assert times['cache_examples.time_fail_second_run'].result == [None]
    assert times['cache_examples.time_fail_second_run'].samples == [None]

    profile_path = join(six.text_type(tmpdir), 'test.profile')
    with open(profile_path, 'wb') as fd:
        fd.write(times['time_secondary.track_value'].profile)
    pstats.Stats(profile_path)

    # Check for running setup on each repeat (one extra run from profile)
    # The output would contain error messages if the asserts in the benchmark fail.
    expected = ["<%d>" % j for j in range(1, 12)]
    assert times['time_examples.TimeWithRepeat.time_it'].stderr.split() == expected

    # Calibration of iterations should not rerun setup
    expected = (['setup']*2, ['setup']*3)
    assert times['time_examples.TimeWithRepeatCalibrate.time_it'].stderr.split() in expected

    # Check tuple-form repeat attribute produced results
    assert 2 <= len(times['time_examples.time_auto_repeat'].samples[0]) <= 4

    # Check run time timestamps
    for name, result in times.items():
        assert result.started_at >= util.datetime_to_js_timestamp(start_timestamp)
        assert result.ended_at >= result.started_at
        assert result.ended_at <= util.datetime_to_js_timestamp(end_timestamp)


def test_quick(benchmarks_fixture):
    # Check that the quick option works
    conf, repo, envs, commit_hash = benchmarks_fixture

    b = benchmarks.Benchmarks.discover(conf, repo, envs, [commit_hash])
    skip_names = [name for name in b.keys() if name != 'time_examples.TimeWithRepeat.time_it']
    b2 = b.filter_out(skip_names)

    results = runner.run_benchmarks(b2, envs[0], quick=True, show_stderr=True)
    times = ResultsWrapper(results, b2)

    assert len(results.get_result_keys(b2)) == 1

    # Check that the benchmark was run only once. The result for quick==False
    # is tested above in test_find_benchmarks
    expected = ["<1>"]
    assert times['time_examples.TimeWithRepeat.time_it'].stderr.split() == expected


def test_skip_param_selection():
    d = {'repo': 'foo'}
    d.update(ASV_CONF_JSON)
    conf = config.Config.from_json(d)

    class DummyEnv(object):
        name = 'env'

    d = [
        {'name': 'test_nonparam', 'params': [], 'version': '1'},
        {'name': 'test_param',
         'params': [['1', '2', '3']],
         'param_names': ['n'],
         'version': '1'}
    ]

    results = Results.unnamed()
    b = benchmarks.Benchmarks(conf, d, [r'test_nonparam', r'test_param\([23]\)'])

    results.add_result(b['test_param'],
                       runner.BenchmarkResult(result=[1, 2, 3], samples=[None]*3, number=[None]*3,
                                              errcode=0, stderr='', profile=None))

    runner.skip_benchmarks(b, DummyEnv(), results)

    assert results._results.get('test_nonparam') == None
    assert results._results['test_param'] == [1, None, None]


@pytest.mark.skipif(not (hasattr(os, 'fork') and hasattr(socket, 'AF_UNIX')),
                    reason="test requires fork and unix sockets")
def test_forkserver(tmpdir):
    tmpdir = six.text_type(tmpdir)
    os.chdir(tmpdir)

    shutil.copytree(BENCHMARK_DIR, 'benchmark')

    d = {}
    d.update(ASV_CONF_JSON)
    d['env_dir'] = "env"
    d['benchmark_dir'] = 'benchmark'
    d['repo'] = 'None'
    conf = config.Config.from_json(d)

    env = environment.ExistingEnvironment(conf, sys.executable, {})
    spawner = runner.ForkServer(env, os.path.abspath('benchmark'))

    result_file = os.path.join(tmpdir, 'run-result')

    try:
        out, errcode = spawner.run('time_examples.TimeWithRepeat.time_it', '{}',
                                   None,
                                   result_file,
                                   60,
                                   os.getcwd())
    finally:
        spawner.close()

    assert out.startswith("<1>")
    assert errcode == 0

    with open(result_file, 'r') as f:
        data = json.load(f)
    assert len(data['samples']) >= 1


def test_table_formatting():
    benchmark = {'params': [], 'param_names': [], 'unit': 's'}
    result = []
    expected = ["[]"]
    assert runner._format_benchmark_result(result, benchmark) == expected

    benchmark = {'params': [['a', 'b', 'c']], 'param_names': ['param1'], "unit": "seconds"}
    result = list(zip([1e-6, 2e-6, 3e-6], [3e-6, 2e-6, 1e-6]))
    expected = ("======== ==========\n"
                " param1            \n"
                "-------- ----------\n"
                "   a      1.00\u00b13\u03bcs \n"
                "   b      2.00\u00b12\u03bcs \n"
                "   c      3.00\u00b11\u03bcs \n"
                "======== ==========")
    table = "\n".join(runner._format_benchmark_result(result, benchmark, max_width=80))
    assert table == expected

    benchmark = {'params': [["'a'", "'b'", "'c'"], ["[1]", "[2]"]], 'param_names': ['param1', 'param2'], "unit": "seconds"}
    result = list(zip([1, 2, None, 4, 5, float('nan')], [None]*6))
    expected = ("======== ======== =======\n"
                "--            param2     \n"
                "-------- ----------------\n"
                " param1    [1]      [2]  \n"
                "======== ======== =======\n"
                "   a      1.00s    2.00s \n"
                "   b      failed   4.00s \n"
                "   c      5.00s     n/a  \n"
                "======== ======== =======")
    table = "\n".join(runner._format_benchmark_result(result, benchmark, max_width=80))
    assert table == expected

    expected = ("======== ======== ========\n"
                " param1   param2          \n"
                "-------- -------- --------\n"
                "   a       [1]     1.00s  \n"
                "   a       [2]     2.00s  \n"
                "   b       [1]     failed \n"
                "   b       [2]     4.00s  \n"
                "   c       [1]     5.00s  \n"
                "   c       [2]      n/a   \n"
                "======== ======== ========")
    table = "\n".join(runner._format_benchmark_result(result, benchmark, max_width=0))
    assert table == expected
