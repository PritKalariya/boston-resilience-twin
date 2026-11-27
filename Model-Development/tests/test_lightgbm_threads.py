import sys
from pathlib import Path

import lightgbm as lgb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers  # noqa: E402, F401  (sets LightGBM's n_jobs=None to follow OMP_NUM_THREADS, #165)


def test_lightgbm_default_threads_follow_omp_in_tests():
    # training_helpers.py: n_jobs=None becomes 0 (OpenMP's default, which follows OMP_NUM_THREADS),
    # not the physical core count; an explicit value keeps LightGBM's own meaning.
    model = lgb.LGBMClassifier()
    assert model._process_n_jobs(None) == 0
    assert model._process_n_jobs(3) == 3
    assert model._process_n_jobs(-1) >= 1


def test_the_patch_is_applied_once():
    assert lgb.LGBMModel._process_n_jobs.follows_omp
    assert lgb.LGBMModel._process_n_jobs(lgb.LGBMClassifier(), None) == 0
