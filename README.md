<a id="top"></a>

<div align="center">

# **SSJ**

<b><ins>A web app that runs a field-service team's work, from the office issuing a job to technicians reporting from site</ins></b>

<br>

![Status](https://img.shields.io/badge/status-completed-orange?style=for-the-badge)
![Main Language](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-all_rights_reserved-lightgrey?style=for-the-badge)

</div>

<br>

<details open>
<summary><b>Table of Contents</b></summary>

1. [Project Overview](#project-overview)
2. [Tech Stack](#tech-stack)
3. [How It Works](#how-it-works)
4. [Skills Learned](#skills-learned)
5. [Challenges and Solutions](#challenges-and-solutions)
6. [Project Structure](#project-structure)
7. [Getting Started](#getting-started)
8. [Extra Things](#extra-things)
9. [Credits and Contact](#credits-and-contact)

</details>

---

## <ins>Project Overview</ins>

> **TL;DR:** A **Flask** web app where users are able to create services and project documents, drags them onto a technician calendar, and technicians sending back daily progress and final reports with photos from their phones.

### ▸ Problem
A field-service team works on many jobs at once, one-off service visits, longer projects with handover documents, and extra work added to running projects. The office has to number and track every document, decide which technicians go where on which day, and collect proof that the work was done. This project was developed since the client wanted to streamline the entire workflow and avoid the complexities of scattered Excel spreadsheets, chat messages, and manual coordination through a central App called **SSJ (Sistem Supervisi Jadwal)**.

### ▸ Objectives
- *Replace scattered spreadsheets with one system* for service visits, projects, handover documents and their status
- *Give the office a scheduling calendar* that shows every technician's work, leave and public holidays for the next 14 days
- *Let technicians report from their phones*, with photos, without needing an office account
- *Keep existing data*: import past documents, clients and accounts from the office's Excel files, and export them back
- *Make it run on one office PC* without IT staff, with automatic start-up and backups

### ▸ Context
I built SSJ during my internship at an Indonesian field-service company, as the only developer on the project. My supervisor gave the starting instructions and the basic workflow. I used Claude Code throughout the project to brainstorm and plan features, and to write and debug code that I then reviewed, tested, and deployed. The original app and its interface were written in Bahasa Indonesia, and the public version is translated into English. Sensitive information such as Company names, document titles, sample data, and the logo were replaced with neutral ones, and the company's print layouts were removed to protect the company's privacy.

<p align="right">(<a href="#top">back to top</a>)</p>

---

## <ins>Tech Stack</ins>

> The tools I **actually used**, what I used them **for**, and what I **learned** using them.

| Category | Tool | What I Used It For |
|:---|:---|:---|
| **Language** | `Python` | The web app, the Windows Controller and the tests |
| **Framework** | `Flask` | The web app, split into blueprints for authentication, documents, scheduling, system and technician pages |
| **Frontend** | `Jinja2`, `HTML`, `CSS`, `JavaScript` | Server-rendered pages, the drag-and-drop scheduling calendar and the phone pages for technicians |
| **Data / Storage** | `SQLAlchemy`, `Alembic`, `PostgreSQL`, `SQLite` | Models and migrations; PostgreSQL in production, SQLite for development and tests |
| **Data Processing** | `pandas`, `openpyxl` | Importing documents, clients and accounts from Excel with a preview step, and exporting lists and the schedule calendar |
| **Desktop** | `PySide6` | The Windows Controller: setup, starting and stopping the server, logs, snapshots and backups |
| **Infra / Deployment** | `Waitress`, Windows Task Scheduler | Serving the app in production on one office PC and starting it and running backups automatically |
| **Libraries** | `Pillow`, `holidays`, `pydantic-settings` | Rotating and shrinking phone photos, filling in public holidays, reading configuration from `.env` |
| **Tools** | `Git`, `unittest`, Claude | Version control, the test suite on in-memory SQLite, planning, brainstorming and translating the app into English |

<p align="right">(<a href="#top">back to top</a>)</p>

---

## <ins>How It Works</ins>

> The office issues a document → schedules it onto technicians → technicians report from site → the office reviews and closes it.

```
Document → Scheduling calendar → Technician app → Office review → Print / Excel export
```

1. **Document.** The office creates a document by hand or imports it from Excel. SSJ numbers it from a configurable format (for example `2026//SVC/1`) and links it to a client site. A PRJ gets its HO automatically, and several projects can be grouped under a Master Project.
2. **Scheduling calendar.** Documents waiting to be scheduled are dragged onto technicians over a 14-day window. The calendar shows leave and public holidays, marks one technician per team as captain, and exports to Excel. An Auto-Suggest button asks a scheduling engine for dates and teams (refer to [Extra Things](#extra-things)).
3. **Technician app.** Technicians log in on their phones and see their calendar and tasks. They send daily progress reports and a final report with photos; SSJ rotates and shrinks the photos before storing them.
4. **Office review.** A final report moves the document to *Pending Approval*. The office approves it to *Completed*, or sends it back, puts it on *Hold*, or marks it *Void*.
5. **Print and export.** Each document type prints with an HTML template the office uploads, and every list exports to Excel.

Office accounts only see the menus they are given, and technicians only see their own assigned tasks.

```mermaid
flowchart LR
    Office[Office browser] --> App
    Phone[Technician phone] --> App
    subgraph PC[Office server PC]
        App[Flask app on Waitress] --> DB[(PostgreSQL)]
        App --> Files[Uploads and print templates]
        Controller[Windows Controller] -. starts, stops, backs up .-> App
        Controller -. snapshots .-> DB
    end
```

<p align="right">(<a href="#top">back to top</a>)</p>

---

## <ins>Skills Learned</ins>

> What this project taught me, technically and beyond.

### ▸ Technical Skills
- **Data modeling:** Designing related tables for clients, documents, schedules and reports, using inheritance so project documents like SVC (Service), PRJ (Project) HO (Handover) and XPRJ (Extra Project) share one base document, and changing the schema with migrations without losing existing data. See `apps/models.py`, `migrations/` and `docs/ssj_schema.dbml`.
- **Access control:** Giving each office account access per menu instead of fixed roles, checking it on every route, and making sure at least one account can always manage access. See `apps/core/auth.py`.
- **Excel import pipelines:** Reading messy real-world spreadsheets, accepting different header names, matching rows to existing clients instead of creating duplicates, and showing a preview before anything is saved. See `apps/services/import_service.py` and `apps/services/prj_import_service.py`.
- **Web security basics:** Hashing passwords, protecting forms with CSRF tokens, limiting login and password-reset attempts, and resetting passwords with a one-time code sent by email. See the CSRF check in `run.py`, `apps/core/rate_limit.py` and the password reset in `apps/authentication/routes.py`.
- **Windows deployment:** Running a web app on an ordinary office PC with a desktop controller that starts the server automatically, checks its health status, and takes scheduled backups. See `controller/`.
- **Testing:** Writing unit tests on an in-memory database and adding a regression test for each bug I fixed. See `tests/`.

### ▸ Soft Skills
- **Gathering requirements:** Regular meetings with office staff and supervisors to create a clear backlog of features and rules
- **Communicating with non-technical users:** Explaining features and errors in general language, and designing pages around how the office already worked
- **Prioritizing:** Deciding what to build first, and what to leave out, so a working version was ready and easily deployed.

<p align="right">(<a href="#top">back to top</a>)</p>

---

## <ins>Challenges and Solutions</ins>

> What went wrong, how I fixed it, and what I took away from the project.

**Matching imported rows to existing clients.** The office's old Excel recaps wrote the same client in many ways, different spellings, missing information (phone numbers, address, etc..), or several branches with the same name. A straight name match either created duplicate clients or merged different sites into one. I settled on a different matching rule per document type (for example, name, client type, contact person, and phone for service visits, but name and address for projects), and added a preview step where the user can pick the right existing client before anything is saved. I learned that real-world data is never as clean as the import code expects, and that letting people confirm uncertain matches beats trying to guess or having the system account for every case automatically.

```powershell
# 1. Clone
git clone https://github.com/hanhansel/SSJ.git
cd SSJ

# 2. Install
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env

# 3. Run
python run.py
```

Then open <http://127.0.0.1:32425>. The first visit asks you to create the first administrator account.

> **Requirements:** Python 3.11+. PostgreSQL and an SMTP account are optional.

- **Database.** With `DATABASE_URL` left empty in `.env`, SSJ uses a local SQLite file under `instance/` and creates the tables on start. To use PostgreSQL, set `DATABASE_URL`, for example `postgresql+psycopg2://ssj_app:password@127.0.0.1:5432/ssj_db`.
- **Email.** Password reset by email needs the SMTP values in `.env`. Everything else works without them.
- **Tests.** Run `python -m unittest discover tests`. They use an in-memory SQLite database and never touch your own data.

<p align="right">(<a href="#top">back to top</a>)</p>

---

## <ins>Extra Things</ins>

> Project-specific details.

<details>
<summary><b>The scheduling engine is not included</b></summary>
<br>

The **Auto-Suggest** button on the Scheduling page asks an engine to propose dates and teams for the selected documents. The original engine used a greedy algorithm experiment that did not produce reliable schedules, so this version ships a stub in its place ([`apps/scheduling/engine/pipeline.py`](apps/scheduling/engine/pipeline.py)): it suggests nothing and lists every document as not scheduled.

Scheduling algorithms are currently being developed on the [`scheduling-algorithms`](../../tree/scheduling-algorithms) branch as Jupyter notebooks that run on synthetic data similar to the app data and format only.

An engine is a function with this signature:

```python
run_engine(tasks, technicians, start_date, holidays, history_ctx, existing_jobs=()) -> dict
```

- [`apps/scheduling/engine_input.py`](apps/scheduling/engine_input.py) builds its input from the database and documents every field.
- It returns `{'scheduled', 'unscheduled', 'runtime_ms', 'search_space_estimate', 'engine_used'}`.
- Each scheduled item carries `project_id`, `doc_type`, `doc_no`, `team`, `start_date`, `work_dates`, `score` and `score_notes`.
- Each unscheduled item carries `project_id`, `doc_type`, `doc_no` and a `reason`.

</details>

<details>
<summary><b>Print templates</b></summary>
<br>

SSJ has no built-in print layouts. Until a template is uploaded, printing a document shows the list of variables available for that document type. To add one:

1. Go to **Settings → Print Templates**.
2. Download the starter file for a document type. It lists every variable you can use.
3. Design the page in HTML with Jinja (`{{ doc.display_no }}`, `{{ doc.client.company_name }}`, loops over items, and so on).
4. Upload it. SSJ checks it with sample data and shows a preview before you confirm.

</details>

<details>
<summary><b>Running it on a Windows server PC</b></summary>
<br>

`python serve.py` runs SSJ with Waitress for production use. The optional Controller app (`pip install -r requirements-controller.txt`, then `python -m controller.main`) prepares the production configuration under `C:\SSJData`. It also starts and stops the server, shows its logs, exports and imports full data snapshots, and can register Windows tasks for automatic start-up and backups.

Devices on the local network open SSJ at `http://<server-ip>:32425`.

</details>

<p align="right">(<a href="#top">back to top</a>)</p>

---

## <ins>Credits and Contact</ins>

**Acknowledgements:** My internship supervisors, for approving the project, explaining the company's workflow, and letting me build and test SSJ on its real processes. [Claude](https://www.anthropic.com/claude) helped me plan features, write and debug code, and translate the app into English.

**License:** No license. All rights reserved. This repository is shared to show my work; please ask before reusing any of it.

**Author:** **Hansel Sebastian**, *Data Science Student @ Bina Nusantara University*

[![GitHub](https://img.shields.io/badge/GitHub-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/hanhansel)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-0A66C2?style=for-the-badge&logo=linkedin&logoColor=white)](https://linkedin.com/in/hansel-sebastian-5a19b8245)

<p align="right">(<a href="#top">back to top</a>)</p>
