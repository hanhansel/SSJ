# SSJ

SSJ is a web application for a field-service team. It covers the whole job, from
the office issuing a work document to technicians reporting from site:

- **Documents.** Service visits (SVC), projects (PRJ) with their handover documents
  (HO), extra project work (XPRJ), and Master Projects that group several PRJs, each
  with a status flow (Online, Pending Approval, Hold, Completed, Void).
- **Scheduling.** A 14-day calendar where documents are dragged onto technicians,
  with leave, public holidays, team captains and an Excel calendar export.
- **Technician app.** Phone pages where technicians see their calendar and tasks
  and upload daily progress and final reports with photos.
- **Clients.** Client sites with a map picker, plus Excel import and export.
- **Accounts.** Office accounts with access per menu, and technician accounts.
- **Print templates.** Each document type prints with an HTML template you upload.
- **Windows Controller.** A desktop app that sets up and runs the server on one
  office PC, with automatic start-up and scheduled backups.

It is built with Flask, SQLAlchemy and Jinja templates, and it runs on SQLite for
development or on PostgreSQL in production.

## About this version

SSJ was built during an internship for an Indonesian field-service company, with
its interface written in Bahasa Indonesia. This public version was translated into
English with the help of [Claude](https://www.anthropic.com/claude), Anthropic's AI
assistant. Company names, document titles, sample data and the logo were replaced
with neutral ones, and the print layouts were removed.

### The scheduling engine is not included

The **Auto-Suggest** button on the Scheduling page asks an engine to propose dates
and teams for the selected documents. The original engine was an early greedy
experiment that did not produce reliable schedules, so this version ships a stub
in its place ([apps/scheduling/engine/pipeline.py](apps/scheduling/engine/pipeline.py)):
it suggests nothing and lists every document as not scheduled. Manual scheduling
by drag and drop works fully.

Scheduling algorithms are developed on the
[`scheduling-algorithms`](../../tree/scheduling-algorithms) branch as Jupyter
notebooks that run on synthetic data only.

An engine is a function with this signature:

```python
run_engine(tasks, technicians, start_date, holidays, history_ctx, existing_jobs=()) -> dict
```

- [apps/scheduling/engine_input.py](apps/scheduling/engine_input.py) builds its
  input from the database and documents every field.
- It returns `{'scheduled', 'unscheduled', 'runtime_ms', 'search_space_estimate',
  'engine_used'}`.
- Each scheduled item carries `project_id`, `doc_type`, `doc_no`, `team`,
  `start_date`, `work_dates`, `score` and `score_notes`.
- Each unscheduled item carries `project_id`, `doc_type`, `doc_no` and a `reason`.

## Running it locally

You need Python 3.11 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python run.py
```

Then open <http://127.0.0.1:32425>. The first visit asks you to create the first
administrator account.

- **Database.** With `DATABASE_URL` left empty in `.env`, SSJ uses a local SQLite
  file under `instance/` and creates the tables on start. To use PostgreSQL instead,
  set `DATABASE_URL`, for example
  `postgresql+psycopg2://ssj_app:password@127.0.0.1:5432/ssj_db`.
- **Email.** Password reset by email needs the SMTP values in `.env`. Everything
  else works without them.
- **Tests.** Run `python -m unittest discover tests`. They use an in-memory SQLite
  database and never touch your own data.

## Print templates

SSJ has no built-in print layouts. Until a template is uploaded, printing a
document shows the list of variables available for that document type. To add one:

1. Go to **Settings → Print Templates**.
2. Download the starter file for a document type. It lists every variable you
   can use.
3. Design the page in HTML with Jinja (`{{ doc.display_no }}`, `{{ doc.client.company_name }}`, loops over items, and so on).
4. Upload it. SSJ checks it with sample data and shows a preview before you
   confirm.

## Running it on a Windows server PC

`python serve.py` runs SSJ with Waitress for production use. The optional
Controller app (`pip install -r requirements-controller.txt`, then
`python -m controller.main`) prepares the production configuration under
`C:\SSJData`. It also starts and stops the server, shows its logs, exports and
imports full data snapshots, and can register Windows tasks for automatic
start-up and backups.

Devices on the local network open SSJ at `http://<server-ip>:32425`.
