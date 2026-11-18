# Scheduling algorithms

This branch is a small library of scheduling algorithms for SSJ's **Auto-Suggest**, with notebooks that explain and compare them. Public `main` ships a stub engine that suggests nothing (see the main README); pick an algorithm from here, or write your own, and plug it in. Everything here runs on **synthetic data only**.

## The algorithms

| Engine | Notebook | Idea | Main options |
|:---|:---|:---|:---|
| `run_greedy` (`research/reference/greedy.py`) | 02 | Place tasks one by one, most important first, each at its best free spot | `lookahead_days` |
| `run_hill_climbing` (`research/reference/hill_climbing.py`) | 03 | Greedy, then keep making small changes that raise the score | `max_stall`, `seed` |
| `run_simulated_annealing` (`research/reference/simulated_annealing.py`) | 04 | Hill climbing that sometimes accepts a worse change, less often over time | `time_limit_s`, `start_temperature`, `end_temperature`, `seed` |
| `run_tabu_search` (`research/reference/tabu_search.py`) | 05 | Always take the best allowed change; recently changed tasks are temporarily forbidden | `time_limit_s`, `tenure`, `neighbours`, `seed` |

Each one takes exactly the same arguments as the app's `run_engine()` (plus its options) and returns the same result, so any of them can replace the stub.

## Using an algorithm in SSJ

1. Copy the `research/` folder into your SSJ checkout, next to `apps/`. From a clone of this repository:

   ```powershell
   git fetch origin scheduling-algorithms
   git checkout origin/scheduling-algorithms -- research
   ```

2. Replace the body of `apps/scheduling/engine/pipeline.py` with, for example:

   ```python
   from research.reference.simulated_annealing import run_simulated_annealing


   def run_engine(tasks, technicians, start_date, holidays, history_ctx, existing_jobs=()) -> dict:
       return run_simulated_annealing(tasks, technicians, start_date, holidays, history_ctx,
                                      existing_jobs, time_limit_s=2.0)
   ```

3. Keep the time limit short (1 to 3 seconds): the Auto-Suggest window waits for the answer.
4. `tests/test_engine_stub.py` checks that the engine is the stub, so delete it or rewrite it for your engine.

The scoring weights (`research/evaluate.py`) are the cold-start values of SSJ's original engine. Adjust them to your team's priorities.

## Writing your own algorithm

1. Write a function with the `run_engine()` signature. Build its result with `research/contract.py` (`scheduled_item`, `unscheduled_item`, `engine_result`) so the format is always right.
2. Reuse the building blocks: `Calendar` and `candidate_score` (`research/schedule_tools.py`) for building schedules, `Schedule` and the moves (`research/local_search.py`) for improving them.
3. Check it with `validate()` (it must return no problems) and compare it with `score()` and `summary()` against the algorithms above, on the same seeds and the same time budget. Notebook 05, section 6, shows how.

## Layout

| Path | What it is |
|:---|:---|
| `research/synthetic.py` | `generate(seed, start_date, scenario)` makes a random but reproducible problem, in exactly the shape of the real engine input |
| `research/contract.py` | Helpers that build the result dicts the app expects |
| `research/evaluate.py` | `validate()` checks the hard rules, `score()` gives the points, `summary()` gives plain numbers to compare algorithms |
| `research/schedule_tools.py` | `Calendar` (who is free when) and `candidate_score()` (what one placement is worth) |
| `research/local_search.py` | `Schedule` (a solution that can be changed and changed back) and the shared moves |
| `research/reference/` | The four algorithms |
| `notebooks/` | One notebook per algorithm, numbered in order |
| `tests/test_research_*.py` | Tests for all of the above |

## The notebooks

1. `01_problem_and_data`: the problem, the synthetic data, and the stub as the starting point
2. `02_greedy_baseline`: tasks placed one by one in priority order (with exercises)
3. `03_hill_climbing`: improving the greedy schedule with small changes (with exercises)
4. `04_simulated_annealing`: sometimes accepting a worse change, so the search doesn't get stuck; settings tuned with Optuna
5. `05_tabu_search`: escaping with memory instead of luck; all four methods compared on the same time budget

## Running it

```powershell
pip install -r requirements-research.txt
python -m nbstripout --install --attributes .gitattributes
python -m unittest discover tests
```

Then open a notebook in VS Code, or run `jupyter notebook` from the repository root.

`nbstripout` removes notebook outputs when a notebook is committed, so only code and text are stored in git. The install line is needed once per clone: `.gitattributes` is committed, but the filter it names lives in each clone's own `.git/config`.

## Rules for contributions

- **Synthetic data only.** Never commit a real database, export or backup.
- **Keep the contract.** An engine must take the same arguments and return the same keys as the stub.
- **Use the shared evaluator.** Change a weight in `research/evaluate.py` only on purpose, and say why in the commit message.
