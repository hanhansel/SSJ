"""Building blocks for local search (notebooks 03 and 04).

A Schedule holds one complete solution that can be changed and changed back.
A move is a function (schedule, rng) -> Change or None that *proposes* one small
change; schedule.apply(change) makes it if it breaks no hard rule, and
schedule.undo(change) reverses it.
"""
from dataclasses import dataclass
from typing import Optional

from research.contract import engine_result, scheduled_item, unscheduled_item, work_dates
from research.evaluate import TEAM_SIZE, as_date, score
from research.reference.greedy import best_placement
from research.schedule_tools import Calendar, score_notes


@dataclass(frozen=True)
class Change:
    """Move one task from `before` to `after`. Each is (team, start), or None for 'not scheduled'."""
    task_id: int
    before: Optional[tuple]
    after: Optional[tuple]


class Schedule:
    """A complete schedule: a (team, start) for every placed task, kept in sync with a Calendar."""

    def __init__(self, data, result):
        self.data = data
        self.tasks = {task['id']: task for task in data['tasks']}
        self.techs = {tech['id']: tech for tech in data['technicians']}
        self.calendar = Calendar(data)
        self.placements = {}            # task_id -> (team, start)
        self.reasons = {}               # task_id -> why it was left unscheduled
        for item in result['scheduled']:
            team = [self.techs[member['id']] for member in item['team']]
            self._book(item['project_id'], team, as_date(item['start_date']))
        for item in result['unscheduled']:
            self.reasons[item['project_id']] = item.get('reason', '')

    # --- reading -----------------------------------------------------------
    @property
    def unscheduled_ids(self):
        return [task_id for task_id in self.tasks if task_id not in self.placements]

    def dates(self, task_id, start):
        return work_dates(start, self.tasks[task_id]['remaining_days'])

    def fits(self, task_id, team, start):
        """Would (team, start) break no hard rule, given everything else that is booked?"""
        task = self.tasks[task_id]
        ids = [tech['id'] for tech in team]
        if task['remaining_days'] < 1 or start < task['earliest_start']:
            return False
        if len(ids) != TEAM_SIZE or len(set(ids)) != TEAM_SIZE:
            return False
        dates = self.dates(task_id, start)
        return all(self.calendar.is_free(tech_id, dates) for tech_id in ids)

    # --- changing ----------------------------------------------------------
    def _book(self, task_id, team, start):
        self.placements[task_id] = (team, start)
        self.calendar.book(team, self.dates(task_id, start), self.tasks[task_id]['location'])

    def _unbook(self, task_id):
        team, start = self.placements.pop(task_id)
        self.calendar.unbook(team, self.dates(task_id, start))

    def apply(self, change):
        """Make the change if it is valid. Returns False, and changes nothing, when it isn't."""
        if change.before is not None:
            self._unbook(change.task_id)
        if change.after is not None:
            team, start = change.after
            if not self.fits(change.task_id, team, start):
                if change.before is not None:
                    self._book(change.task_id, *change.before)
                return False
            self._book(change.task_id, team, start)
        return True

    def undo(self, change):
        """Reverse a change that apply() made."""
        if change.after is not None:
            self._unbook(change.task_id)
        if change.before is not None:
            self._book(change.task_id, *change.before)

    def snapshot(self):
        """A copy of every placement, to come back to later with restore()."""
        return dict(self.placements)

    def restore(self, snapshot):
        """Go back to a schedule saved with snapshot()."""
        for task_id in list(self.placements):
            self._unbook(task_id)
        for task_id, (team, start) in snapshot.items():
            self._book(task_id, team, start)

    # --- scoring and output ------------------------------------------------
    def _light_result(self):
        """Just enough of a result for score(): dates stay date objects, no text."""
        scheduled = [{'project_id': task_id, 'team': [{'id': tech['id']} for tech in team],
                      'start_date': start, 'work_dates': self.dates(task_id, start)}
                     for task_id, (team, start) in self.placements.items()]
        return {'scheduled': scheduled, 'unscheduled': [{'project_id': task_id} for task_id in self.unscheduled_ids]}

    def total(self):
        """The grader's score of the schedule as it is now."""
        return score(self._light_result(), self.data).total

    def to_result(self, engine_used, started):
        """The schedule as run_engine() returns it, with per-task points and notes."""
        final = score(self._light_result(), self.data)
        scheduled = []
        for task_id, (team, start) in sorted(self.placements.items(), key=lambda pair: (pair[1][1], pair[0])):
            parts = final.per_task[task_id]
            scheduled.append(scheduled_item(self.tasks[task_id], team, start, sum(parts.values()), score_notes(parts)))
        unscheduled = [unscheduled_item(self.tasks[task_id], self.reasons.get(task_id) or 'No free team was found.')
                       for task_id in self.unscheduled_ids]
        return engine_result(scheduled, unscheduled, engine_used, started)


# --- moves -----------------------------------------------------------------

def random_placed_task(schedule, rng):
    """A random scheduled task id, or None when nothing is scheduled."""
    return rng.choice(list(schedule.placements)) if schedule.placements else None


def replace_move(schedule, rng):
    """Swap one technician of a random task for a random technician outside its team."""
    task_id = random_placed_task(schedule, rng)
    if task_id is None:
        return None
    team, start = schedule.placements[task_id]
    in_team = {tech['id'] for tech in team}
    others = [tech for tech in schedule.data['technicians'] if tech['id'] not in in_team]
    if not others:
        return None
    new_team = list(team)
    new_team[rng.randrange(len(team))] = rng.choice(others)
    return Change(task_id, (team, start), (new_team, start))


def insert_move(schedule, rng):
    """Place a random unscheduled task at its best free spot on the current calendar."""
    waiting = [task_id for task_id in schedule.unscheduled_ids if schedule.tasks[task_id]['remaining_days'] >= 1]
    if not waiting:
        return None
    task_id = rng.choice(waiting)
    found = best_placement(schedule.tasks[task_id], schedule.data, schedule.calendar)
    if found is None:
        return None
    _points, team, start = found[:3]
    return Change(task_id, None, (team, start))
