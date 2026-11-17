from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timedelta, timezone
from functools import cached_property
from sqlalchemy import event
from sqlalchemy.orm import Session

def wib_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=7)

db = SQLAlchemy()


# Document workflow (SVC, XPRJ, PRJ). Online and Pending Approval are active;
# Completed and Void are final: once set, the status never changes again.
# Hold is a paused Online document: hidden from the scheduler and technicians,
# listed only under the Hold filter, and resumed by setting it back to Online.
STATUS_ONLINE = 'Online'
STATUS_PENDING = 'Pending Approval'
STATUS_DONE = 'Completed'
STATUS_VOID = 'Void'
STATUS_HOLD = 'Hold'
ACTIVE_STATUSES = (STATUS_ONLINE, STATUS_PENDING)
FINAL_STATUSES = (STATUS_DONE, STATUS_VOID)
# Statuses that can never be given a schedule block.
UNSCHEDULABLE_STATUSES = FINAL_STATUSES + (STATUS_HOLD,)

# A HO mirrors its PRJ: Approved when the PRJ is Completed, Void when it is Void,
# Hold when it is Hold.
HO_APPROVED = 'Approved'


# ==========================================
# 1. USER, ACCOUNT TYPE & MENU ACCESS
# ==========================================
# The account type is fixed at creation.  A technician works from the phone app
# and is scheduled; an Office account sees only the menus ticked for it.
ROLE_OFFICE = 'Office'
ROLE_TECHNICIAN = 'Technician'

# Menus an office account can be given, in sidebar order: key -> label.
MENU_DOCUMENTS = 'documents'
MENU_MASTER_PROJECT = 'master_project'
MENU_STORAGE = 'storage'
MENU_SCHEDULING = 'scheduling'
MENU_SETTINGS = 'settings'
MENU_ACCESS = 'access'
MENUS = {
    MENU_DOCUMENTS: 'Documents',
    MENU_MASTER_PROJECT: 'Master Project',
    MENU_STORAGE: 'Data Storage',
    MENU_SCHEDULING: 'Scheduling',
    MENU_SETTINGS: 'Settings',
    MENU_ACCESS: 'Access Management',
}
ALL_MENUS = tuple(MENUS)
# What each menu owns, shown under its checkbox in Access Management.
MENU_DESCRIPTIONS = {
    MENU_DOCUMENTS: 'Create, edit, import and approve active documents (SVC, XPRJ, PRJ, HO)',
    MENU_MASTER_PROJECT: 'View and manage Master Projects',
    MENU_STORAGE: 'Client Data and the archive of completed documents (view only)',
    MENU_SCHEDULING: 'Technician calendar, leave, holidays and day estimates',
    MENU_SETTINGS: 'System settings and print templates',
    MENU_ACCESS: 'Manage accounts and menu access',
}

# Every table belongs to one menu: other pages may show and pick its rows, but
# only its owner menu may add or change them.  An element on one menu's page
# that writes to another menu's table is listed here and needs all its menus.
# Routes check it with User.can_do, templates hide it with can_do(), and
# Manajemen Akses lists it in the access summary.
CROSS_MENU_ACTIONS = {
    'add_client_from_document': (
        (MENU_DOCUMENTS, MENU_STORAGE), 'Add new clients while creating or importing documents'),
}


class Role(db.Model):
    __tablename__ = 'roles'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)  # Office | Technician
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255))


class UserMenuAccess(db.Model):
    """One menu an office account may use."""
    __tablename__ = 'user_menu_access'
    __table_args__ = (db.UniqueConstraint('user_id', 'menu', name='uq_user_menu_access_user_menu'),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    menu = db.Column(db.String(40), nullable=False)


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)

    role_id = db.Column(db.Integer, db.ForeignKey('roles.id'), nullable=False, index=True)
    role_record = db.relationship('Role', backref=db.backref('users', lazy=True))

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=wib_now)

    schedule_blocks = db.relationship('ScheduleBlock', backref='technician', lazy=True)
    uploaded_reports = db.relationship('WorkReport', backref='uploader', lazy=True)
    menu_access = db.relationship('UserMenuAccess', lazy='selectin', cascade='all, delete-orphan')

    @property
    def role(self):
        """The account type code: Office or Technician."""
        return self.role_record.code if self.role_record else ''

    @property
    def is_office(self):
        return self.role == ROLE_OFFICE

    @property
    def menus(self):
        """Menu keys this account may use, in sidebar order; a technician has none."""
        granted = {row.menu for row in self.menu_access}
        return [menu for menu in ALL_MENUS if menu in granted] if self.is_office else []

    @menus.setter
    def menus(self, menus):
        wanted = [menu for menu in ALL_MENUS if menu in set(menus or ())]
        kept = [row for row in self.menu_access if row.menu in wanted]
        have = {row.menu for row in kept}
        self.menu_access = kept + [UserMenuAccess(menu=menu) for menu in wanted if menu not in have]

    @property
    def is_superadmin(self):
        """Superadmin is shorthand for an office account with every menu, not a stored type."""
        return self.is_office and set(self.menus) == set(ALL_MENUS)

    @property
    def access_label(self):
        if not self.is_office:
            return ROLE_TECHNICIAN
        return 'Superadmin' if self.is_superadmin else 'Admin'

    def can(self, *menus):
        """True when this office account has at least one of the menus."""
        return self.is_office and any(menu in self.menus for menu in menus)

    def can_do(self, action):
        """True when this account has every menu a CROSS_MENU_ACTIONS entry needs."""
        menus, _label = CROSS_MENU_ACTIONS[action]
        return self.is_office and all(menu in self.menus for menu in menus)

    def can_open_document(self, status):
        """Active documents belong to Documents; finished ones are also in Data Storage's archive."""
        if status in FINAL_STATUSES:
            return self.can(MENU_DOCUMENTS, MENU_STORAGE)
        return self.can(MENU_DOCUMENTS)


class PasswordResetOTP(db.Model):
    """Server-side state for one password reset request."""
    __tablename__ = 'password_reset_otps'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    otp_hash = db.Column(db.String(64), nullable=False)
    otp_salt = db.Column(db.String(32), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=wib_now)


class AuthRateLimit(db.Model):
    """Persistent per-subject counters for login and reset-request throttling."""
    __tablename__ = 'auth_rate_limits'
    __table_args__ = (
        db.UniqueConstraint('action', 'subject', name='uq_auth_rate_limits_action_subject'),
        db.Index('ix_auth_rate_limits_last_attempt', 'last_attempt'),
    )

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(40), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    window_started = db.Column(db.DateTime, nullable=False, default=wib_now)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_attempt = db.Column(db.DateTime, nullable=False, default=wib_now)




# ==========================================
# 2. CLIENT SITES (Clients / Locations)
# ==========================================
class ClientSite(db.Model):
    __tablename__ = 'client_sites'
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.Text)
    client_type = db.Column(db.String(50), nullable=True)  # 'On-Call', 'Survey', 'Temp', 'Contract'
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    pic_name = db.Column(db.String(100))
    pic_phone = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=wib_now)

    # All document variants share one internal document ID.
    documents = db.relationship('Document', backref='client', lazy=True)

    @property
    def is_contract(self):
        """Contract clients' SVC are scheduled first and labelled Urgent."""
        return (self.client_type or '').strip().lower() == 'contract'


# ==========================================
# 3. ENDOSKELETON: MASTER PROJECT & PRJ
# Note: Designed as future-proof structures.
# PRJ and MasterProject code is ready to wear the suit later.
# ==========================================
class MasterProject(db.Model):
    __tablename__ = 'master_projects'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('client_sites.id'), nullable=False, index=True)
    client = db.relationship('ClientSite', backref=db.backref('master_projects', lazy=True))
    site_address = db.Column(db.Text)
    work_scope = db.Column(db.Text)  # "Pekerjaan": QUOT code or task titles of the project
    notes = db.Column(db.Text)
    # 'Online', 'Hold', 'Completed'.  Hold pauses the project: its PRJs keep their own
    # status but leave the scheduler backlog and the technician app until it is Online again.
    status = db.Column(db.String(50), default='Online')
    created_at = db.Column(db.DateTime, default=wib_now)


class Document(db.Model):
    """Shared identity and common fields for every business document.

    ``id`` is the permanent FK target.  ``doc_no`` is only the numeric business
    sequence; its printable representation is derived from type/date/settings.
    """
    __tablename__ = 'documents'
    __table_args__ = (
        db.UniqueConstraint('document_type', 'doc_no', 'doc_period',
                           name='uq_document_type_no_period'),
    )

    id = db.Column(db.Integer, primary_key=True)
    document_type = db.Column(db.String(20), nullable=False)
    doc_no = db.Column(db.Integer, nullable=False)
    doc_period = db.Column(db.String(10), nullable=False, default='all')
    # Printable number frozen at creation so later format/padding changes in
    # SystemSetting never rewrite the number of an issued document.
    doc_number_text = db.Column(db.String(100), nullable=True)
    date = db.Column(db.Date, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('client_sites.id'), nullable=False)
    site_address = db.Column(db.Text)
    status = db.Column(db.String(50), default='Online')
    created_at = db.Column(db.DateTime, default=wib_now)
    # Team lead among the scheduled technicians; display only, grants no extra rights.
    captain_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)

    __mapper_args__ = {'polymorphic_on': document_type, 'polymorphic_identity': 'document'}

    @property
    def display_no(self):
        if self.doc_number_text:
            return self.doc_number_text
        from apps.core.registry import format_document_number
        return format_document_number(self.document_type, self.doc_no, self.date)

    @property
    def is_on_hold(self):
        """Paused: Hold itself, or an PRJ whose Master Project is on Hold (its own status unchanged)."""
        project = getattr(self, 'master_project', None)
        return self.status == STATUS_HOLD or (project is not None and project.status == STATUS_HOLD)

    @property
    def scheduled_end_date(self):
        """Returns the latest end date among all schedule blocks."""
        if not self.schedule_blocks:
            return None
        latest = None
        for b in self.schedule_blocks:
            end_date = b.start_date + timedelta(days=max(0, b.days_duration - 1))
            if latest is None or end_date > latest:
                latest = end_date
        return latest

    @property
    def unique_scheduled_days(self):
        """Returns the number of unique calendar dates that have been scheduled."""
        if not self.schedule_blocks:
            return 0
        unique_dates = set()
        for b in self.schedule_blocks:
            for i in range(max(1, b.days_duration)):
                unique_dates.add(b.start_date + timedelta(days=i))
        return len(unique_dates)

    @cached_property
    def _blocks(self):
        """Single DB query shared by all schedule-derived properties."""
        return ScheduleBlock.query.filter_by(document_id=self.id).all()

    @property
    def captain_name(self):
        """The captain only counts while they still have a block on this document."""
        if not self.captain_id:
            return None
        return next((b.technician.name for b in self._blocks
                     if b.user_id == self.captain_id and b.technician), None)

    @property
    def assigned_technicians(self):
        techs = sorted({b.technician.name for b in self._blocks if b.technician})
        if not techs:
            return "-"
        captain = self.captain_name
        if captain in techs:
            techs.remove(captain)
            techs.insert(0, f'{captain} (Captain)')
        return ", ".join(techs)

    @property
    def scheduled_start_date(self):
        return min([b.start_date for b in self._blocks]) if self._blocks else None

    @property
    def scheduled_duration(self):
        return max([b.days_duration for b in self._blocks]) if self._blocks else None

    @property
    def scheduled_target_days(self):
        """Count each calendar day scheduled for this document only once.

        A document can have one overlapping block per technician.  Those
        overlapping days represent one team schedule, not extra document days.
        Separate blocks still contribute their own dates.
        """
        scheduled_dates = {
            block.start_date + timedelta(days=day_index)
            for block in self._blocks
            for day_index in range(block.days_duration)
        }
        return len(scheduled_dates) if scheduled_dates else None

    @property
    def latest_report(self):
        # A completed document's canonical report is always its one final report.
        return WorkReport.query.filter_by(document_id=self.id, report_type='final').first()


class DocumentPRJ(Document):
    __tablename__ = 'doc_prj'
    id = db.Column(db.Integer, db.ForeignKey('documents.id'), primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    master_project_id = db.Column(db.Integer, db.ForeignKey('master_projects.id'), nullable=True, index=True)
    master_project = db.relationship('MasterProject', backref=db.backref('prjs', lazy=True))
    # Free text: replacement PRJs may reuse the SO of a voided one, so no uniqueness.
    so_number = db.Column(db.String(200), nullable=False)
    notes = db.Column(db.Text)
    urgency_level = db.Column(db.Integer, default=1)  # 1 = Normal, 2 = Urgent
    estimated_calendar_days = db.Column(db.Integer, default=1)
    actual_completion = db.Column(db.Date, nullable=True)
    xprj_children = db.relationship('DocumentXPRJ', backref='prj_parent', lazy=True,
                                    foreign_keys='DocumentXPRJ.prj_id')

    __mapper_args__ = {'polymorphic_identity': 'prj'}


# ==========================================
# 4. ACTIVE DOCUMENTS: SVC (On-Call / Maintenance)
# ==========================================
class DocumentSVC(Document):
    __tablename__ = 'doc_svc'
    id = db.Column(db.Integer, db.ForeignKey('documents.id'), primary_key=True)
    reference = db.Column(db.String(200))
    # Core content fields (Paragraph free-text)
    customer_complaint = db.Column(db.Text)
    task_description = db.Column(db.Text)
    notes = db.Column(db.Text)

    # Operational metrics
    estimated_calendar_days = db.Column(db.Integer, default=2)
    actual_completion = db.Column(db.Date, nullable=True)

    # Workflow statuses: see ACTIVE_STATUSES / FINAL_STATUSES at the top of this module.
    __mapper_args__ = {'polymorphic_identity': 'svc'}


# ==========================================
# 5. ACTIVE DOCUMENTS: XPRJ (Extra Project Document)
# ==========================================
class DocumentXPRJ(Document):
    __tablename__ = 'doc_xprj'
    id = db.Column(db.Integer, db.ForeignKey('documents.id'), primary_key=True)
    prj_id = db.Column(db.Integer, db.ForeignKey('doc_prj.id'), nullable=True)  # Nullable endoskeleton FK
    reference = db.Column(db.String(200))                                        # Reference Number (SO/PO Ref)
    # Core content fields (Paragraph free-text)
    customer_complaint = db.Column(db.Text)
    task_description = db.Column(db.Text)
    notes = db.Column(db.Text)

    estimated_calendar_days = db.Column(db.Integer, default=2)
    actual_completion = db.Column(db.Date, nullable=True)
    __mapper_args__ = {'polymorphic_identity': 'xprj'}


# ==========================================
# 6. UNIFIED LINE ITEMS (DocumentLists)
# Note: Single table for multi-item lists (PRJ, XPRJ, HO)
# ==========================================
class PRJItem(db.Model):
    __tablename__ = 'prj_items'
    __table_args__ = (db.Index('ix_prj_items_prj', 'prj_id'),)

    id = db.Column(db.Integer, primary_key=True)
    prj_id = db.Column(db.Integer, db.ForeignKey('doc_prj.id'), nullable=False)
    prj = db.relationship('DocumentPRJ', backref=db.backref('items', lazy=True))
    description = db.Column(db.Text, nullable=False)
    qty = db.Column(db.Integer, default=1)
    uom = db.Column(db.String(50), default='Unit')       # Pcs, Unit, Meter, Titik, Set
    display_order = db.Column(db.Integer, default=0)


class PRJTechnicianTask(db.Model):
    __tablename__ = 'prj_technician_tasks'
    id = db.Column(db.Integer, primary_key=True)
    prj_id = db.Column(db.Integer, db.ForeignKey('doc_prj.id'), nullable=False)
    prj = db.relationship('DocumentPRJ', backref=db.backref('technician_tasks', lazy=True))
    description = db.Column(db.Text, nullable=False)
    display_order = db.Column(db.Integer, default=0)


# ==========================================
# 7. SCHEDULING (Polymorphic Calendar Blocks)
# ==========================================
class ScheduleBlock(db.Model):
    __tablename__ = 'schedule_blocks'
    __table_args__ = (db.Index('ix_schedule_blocks_document', 'document_id'),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.id'), nullable=True)
    document = db.relationship('Document', backref=db.backref('schedule_blocks', lazy=True))

    start_date = db.Column(db.Date, nullable=False)
    days_duration = db.Column(db.Integer, default=1, nullable=False)
    status = db.Column(db.String(50), default='Scheduled')  # 'Scheduled' | 'Done' | 'Cancelled'
    leave_reason = db.Column(db.String(200), nullable=True)
    notes = db.Column(db.Text)

    @property
    def doc_type(self):
        return self.document.document_type if self.document else 'leave'

    @property
    def doc_id(self):
        return self.document_id or 0


# ==========================================
# 9. WORK REPORTS & ATTACHMENTS
# ==========================================
class WorkReport(db.Model):
    __tablename__ = 'work_reports'
    __table_args__ = (
        db.Index('ix_work_reports_document', 'document_id'),
        # Daily reports are unlimited; a document has exactly one final report.
        db.Index(
            'uq_work_reports_one_final_per_document', 'document_id', unique=True,
            postgresql_where=db.text("report_type = 'final'"),
            sqlite_where=db.text("report_type = 'final'"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.id'), nullable=False)
    document = db.relationship('Document', backref=db.backref('work_reports', lazy=True))
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=wib_now)
    report_type = db.Column(db.String(20), default='final')  # 'daily' | 'final'
    # Required by the submit form; nullable only so reports created before
    # this column existed remain valid.
    report_title = db.Column(db.String(200), nullable=True)
    report_text = db.Column(db.Text)
    status = db.Column(db.String(50), default='Pending')  # 'Daily', 'Pending', 'Approved'
    photos = db.relationship(
        'WorkReportPhoto', backref='report', lazy='selectin',
        cascade='all, delete-orphan', order_by='WorkReportPhoto.id'
    )

    @property
    def doc_type(self):
        return self.document.document_type if self.document else None

    @property
    def doc_id(self):
        return self.document_id

    @property
    def photo_list(self):
        return [photo.stored_path for photo in self.photos]


class WorkReportPhoto(db.Model):
    """Metadata for one physical evidence file stored under UPLOAD_FOLDER."""
    __tablename__ = 'work_report_photos'
    __table_args__ = (
        db.UniqueConstraint('stored_path', name='uq_work_report_photos_stored_path'),
        db.Index('ix_work_report_photos_report_id', 'report_id'),
    )

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('work_reports.id'), nullable=False)
    stored_path = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer, nullable=True)
    uploaded_at = db.Column(db.DateTime, default=wib_now, nullable=False)
    # Read from the photo before its metadata is stripped (phone clock); None when it has none.
    taken_at = db.Column(db.DateTime, nullable=True)


# ==========================================
# 9b. HO (Handover Document) — PRJ only
# ==========================================
class HO(db.Model):
    """Handover document that always belongs to exactly one PRJ.

    It is created together with its PRJ and shares the PRJ's sequence number
    and date, so it has no numbering of its own.  Line items are NOT stored
    here; they always mirror ``prj.items``.
    Status flow follows the PRJ: Online -> Pending Approval (technician final
    upload) -> Approved (PRJ Completed), or Void (PRJ Void).
    """
    __tablename__ = 'hos'

    id = db.Column(db.Integer, primary_key=True)
    ho_number_text = db.Column(db.String(100), nullable=True)  # frozen printable number
    date = db.Column(db.Date, nullable=False, default=lambda: wib_now().date())
    prj_id = db.Column(db.Integer, db.ForeignKey('doc_prj.id'), unique=True, nullable=False)

    status = db.Column(db.String(50), nullable=False, default='Online')
    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=wib_now)
    uploaded_at = db.Column(db.DateTime, nullable=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    completion_date = db.Column(db.Date, nullable=True)

    prj = db.relationship('DocumentPRJ', backref=db.backref('ho', uselist=False, lazy=True))
    uploader = db.relationship('User', foreign_keys=[uploaded_by])
    approver = db.relationship('User', foreign_keys=[approved_by])
    photos = db.relationship(
        'HOPhoto', backref='ho', lazy='selectin',
        cascade='all, delete-orphan', order_by='HOPhoto.id'
    )

    @property
    def display_no(self):
        if self.ho_number_text:
            return self.ho_number_text
        from apps.core.registry import format_ho_number
        return format_ho_number(self.prj.doc_no, self.prj.date)

    @property
    def items(self):
        """HO items always mirror its PRJ."""
        return sorted(self.prj.items, key=lambda i: i.display_order or 0)

    @property
    def photo_list(self):
        return [photo.stored_path for photo in self.photos]

    @property
    def is_locked(self):
        return self.status in (HO_APPROVED, STATUS_VOID)


@event.listens_for(Session, 'before_flush')
def give_every_new_prj_its_ho(session, flush_context, instances):
    """Every PRJ owns exactly one HO, created with it and numbered and dated like it."""
    from apps.core.registry import format_ho_number
    with session.no_autoflush:
        for prj in [obj for obj in session.new if isinstance(obj, DocumentPRJ) and obj.ho is None]:
            if prj.status == STATUS_DONE:
                ho_status = HO_APPROVED
            else:
                ho_status = prj.status or STATUS_ONLINE
            session.add(HO(
                prj=prj,
                ho_number_text=format_ho_number(prj.doc_no, prj.date),
                date=prj.date,
                status=ho_status,
                notes='',
            ))


class HOPhoto(db.Model):
    """Photo of the physically signed HO paper."""
    __tablename__ = 'ho_photos'
    __table_args__ = (
        db.UniqueConstraint('stored_path', name='uq_ho_photos_stored_path'),
        db.Index('ix_ho_photos_ho_id', 'ho_id'),
    )

    id = db.Column(db.Integer, primary_key=True)
    ho_id = db.Column(db.Integer, db.ForeignKey('hos.id'), nullable=False)
    stored_path = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer, nullable=True)
    uploaded_at = db.Column(db.DateTime, default=wib_now, nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    taken_at = db.Column(db.DateTime, nullable=True)  # see WorkReportPhoto.taken_at


# ==========================================
# 10. DYNAMIC SYSTEM SETTINGS ENGINE
# ==========================================
class SystemSetting(db.Model):
    __tablename__ = 'system_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), default='general')
    description = db.Column(db.String(200))

    @classmethod
    def get_val(cls, key, default=''):
        item = cls.query.filter_by(key=key).first()
        return item.value if item else default

    @classmethod
    def set_val(cls, key, value, category='general', description=''):
        item = cls.query.filter_by(key=key).first()
        if not item:
            item = cls(key=key, value=value, category=category, description=description)
            db.session.add(item)
        else:
            item.value = value
            if category:
                item.category = category
            if description:
                item.description = description
        db.session.commit()


# (key, default value, category, description). Seeded on first start and restored
# by the "Reset to Defaults" button in Settings.
DEFAULT_SYSTEM_SETTINGS = (
    ('svc_format', '{NO}/{MM}/SVC/{YY}', 'format', 'SVC document number format'),
    ('xprj_format', '{NO}/{MM}/XPRJ/{YY}', 'format', 'XPRJ document number format'),
    ('prj_format', '{NO}/PRJ/{YY}', 'format', 'PRJ document number format'),
    ('ho_format', '{NO}/HO/{YY}', 'format', 'HO document number format (the sequence number follows the PRJ)'),
    ('doc_no_padding', '3', 'format', 'Number of digits in the sequence number (padding)'),
    ('doc_no_reset_period', 'Yearly', 'format', 'Sequence number reset period (Monthly / Yearly / Never)'),
    ('doc_upload_folder_format', 'documents/{DOC_TYPE}/{YEAR}/{DOC_NO}', 'upload', 'Document upload folder structure format'),
    ('doc_upload_filename_format', '{TIMESTAMP}_{UUID}_{ORIGINAL_NAME}', 'upload', 'Document upload file name format'),
)


# ==========================================
# 11. SYSTEM HOLIDAYS (Holiday Calendar)
# ==========================================
class SystemHoliday(db.Model):
    __tablename__ = 'system_holidays'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    description = db.Column(db.String(100), nullable=False)
