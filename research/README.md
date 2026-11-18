# Scheduling research

This branch is where scheduling algorithms for SSJ's **Auto-Suggest** are developed. Public `main` ships a stub engine instead (see the main README). Everything here runs on **synthetic data only**.

## The goal

Write a `run_engine()` that can replace the stub in `apps/scheduling/engine/pipeline.py` without changing anything else in the app. It takes the input that `apps/scheduling/engine_input.py` builds, and returns the result the Auto-Suggest window shows.

## Layout

| Path | What it is |
|:---|:---|
| `research/synthetic.py` | `generate(seed, start_date, scenario)` makes a random but reproducible problem, in exactly the shape of the real engine input |
| `research/contract.py` | Helpers that build the result dicts the app expects (`scheduled_item`, `unscheduled_item`, `engine_result`) |
| `research/evaluate.py` | `validate()` checks the hard rules, `score()` gives the points, `summary()` gives plain numbers to compare algorithms |
| `notebooks/` | One notebook per step, numbered in order |
| `tests/test_research_tools.py` | Tests for the generator and the evaluator |

## Plan

1. `01_problem_and_data`: the problem, the synthetic data, and the stub as the starting point
2. `02_greedy_baseline`: tasks placed one by one in priority order
3. `03_local_search`: improving the greedy schedule with simulated annealing or tabu search
4. `04_weight_tuning`: tuning the scoring weights with Optuna

Every algorithm is judged with the same evaluator, on the same seeds.

## Running it

```powershell
pip install -r requirements-research.txt
python -m nbstripout --install --attributes .gitattributes
python -m unittest discover tests
```

Then open a notebook in VS Code, or run `jupyter notebook` from the repository root.

`nbstripout` removes notebook outputs when a notebook is committed, so only code and text are stored in git. The install line is needed once per clone: `.gitattributes` is committed, but the filter it names lives in each clone's own `.git/config`.

## Rules

- **Synthetic data only.** Never load a real database, export or backup in this branch.
- **Keep the contract.** An engine must take the same arguments and return the same keys as the stub.
- **Use the shared evaluator.** Change a weight in `research/evaluate.py` only on purpose, and say why in the commit message.
