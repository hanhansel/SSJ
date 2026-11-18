"""Building blocks shared by the algorithms: who is free when, and what a placement is worth."""
from collections import defaultdict

from research.contract import scheduled_item, work_dates
from research.evaluate import days_from, score_item

NOTE_LABELS = {
    'priority': 'Priority',
    'aging': 'Waited long',
    'familiarity': 'Knows the client',
    'experience': 'Experience with this type',
    'chemistry': 'Team has worked together',
    'proximity': 'Near yesterday\'s job',
    'sunday': 'Works on a Sunday',
    'holiday': 'Works on a holiday',
    'saturday_start': 'Starts on a Saturday',
    'wait': 'Starts later than possible',
}


class Calendar:
    """Which days each technician is taken, and where they work each day.

    Starts from the jobs already on the calendar; book() adds new ones.
    """

    def __init__(self, data):
        self.technicians = data['technicians']
        self.busy = defaultdict(set)    # tech_id -> set of dates
        self.where = {}                 # (tech_id, date) -> (lat, lng) or None
        for job in data.get('existing_jobs', ()):
            self.book([{'id': job['tech_id']}], days_from(job['start_date'], job['days_duration']), job['location'])

    def is_free(self, tech_id, dates):
        return self.busy[tech_id].isdisjoint(dates)

    def free_technicians(self, dates):
        """The technicians free on every one of `dates`."""
        return [tech for tech in self.technicians if self.is_free(tech['id'], dates)]

    def book(self, team, dates, location):
        for tech in team:
            self.busy[tech['id']].update(dates)
            for day in dates:
                self.where[(tech['id'], day)] = location

    def unbook(self, team, dates):
        for tech in team:
            self.busy[tech['id']].difference_update(dates)
            for day in dates:
                self.where.pop((tech['id'], day), None)


def candidate_score(task, team, start, data, calendar):
    """(points, parts) of doing `task` with `team` from `start`, given what is already booked."""
    item = scheduled_item(task, team, start)
    parts = score_item(item, task, data, calendar.where)
    return sum(parts.values()), parts


def score_notes(parts):
    """Readable lines for the Auto-Suggest window, biggest effect first."""
    lines = []
    for name, points in sorted(parts.items(), key=lambda pair: -abs(pair[1])):
        if round(points):
            lines.append(f'{NOTE_LABELS.get(name, name)}: {points:+.0f}')
    return lines


__all__ = ['Calendar', 'candidate_score', 'score_notes', 'work_dates']
