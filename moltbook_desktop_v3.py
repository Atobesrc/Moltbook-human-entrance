import json
import threading
from typing import Any, Callable, List, Optional
import re

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit, QPlainTextEdit, QListWidget, QComboBox,
    QSpinBox, QTabWidget, QGroupBox, QMessageBox, QFileDialog, QSplitter, QCheckBox,
    QToolButton, QMenu, QInputDialog
)

from moltbook_client import MoltbookClient
from moltbook_util import (
    CRED_PATH, load_creds, save_creds, normalize_submolt, parse_json,
    extract_agent_name, extract_posts_list, extract_results_list, extract_post_obj, extract_comments_list
)


# ---------- UI Bus (thread-safe via queued connections) ----------
class UiBus(QObject):
    log_line = Signal(str)
    activity = Signal(str)
    error = Signal(str)
    invoke = Signal(object)  # emits a callable to be executed on UI thread


def safe_slot(fn):
    """Decorator: prevents unhandled slot exceptions from destabilizing/closing the app."""
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as e:
            try:
                self.bus.error.emit(str(e))
                self.bus.log_line.emit(f"[EXCEPTION] {fn.__name__}: {e}")
            except Exception:
                QMessageBox.critical(self, "Error", str(e))
    return wrapper


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.bus = UiBus()
        self.bus.log_line.connect(self._append_log_ui)
        self.bus.activity.connect(self.activity_label_set)
        self.bus.error.connect(lambda s: QMessageBox.critical(self, "Error", s))
        self.bus.invoke.connect(lambda fn: fn())  # queued to UI thread automatically

        self.client = MoltbookClient()
        self.client.debug_hook = lambda s: self.bus.log_line.emit(s)

        self.creds = load_creds()
        self.agent_name_cached = self.creds.get("agent_name", "")

        # State
        self.posts: List[dict] = []
        self.search_results: List[dict] = []
        self.comments: List[dict] = []
        self.selected_post_id: Optional[str] = None

        # Quick-post collapse state
        self._quickpost_collapsed: bool = False
        self._quickpost_prev_sizes: Optional[List[int]] = None

        self._build_ui()
        self.apply_glass_dark_theme()
        self._load_saved_key()

        self.setWindowTitle("Moltbook Human Entrance")
        self.resize(1400, 880)

    # ----- UI thread helpers -----
    def activity_label_set(self, msg: str):
        self.activity_label.setText(msg)

    def ui_invoke(self, fn: Callable[[], None]):
        self.bus.invoke.emit(fn)

    def set_activity(self, msg: str):
        self.bus.activity.emit(msg)

    def clear_activity(self):
        self.bus.activity.emit("")

    # ----- Log helpers (ONLY most current action) -----
    def _start_action_log(self, title: str):
        # Clear log each action; keep only this action’s log.
        try:
            self.log_output.setPlainText(f"== {title} ==\n")
        except Exception:
            pass

    def run_bg(
        self,
        activity: str,
        fn: Callable[[], Any],
        done: Callable[[Any], None],
        on_finish: Optional[Callable[[], None]] = None,  # NEW: runs on UI thread for both success/error
    ):
        # Clear log for this action
        self._start_action_log(activity)
        self.set_activity(activity)

        def runner():
            try:
                res = fn()
            except Exception as e:
                err = str(e)

                def apply_err():
                    try:
                        self.clear_activity()
                        self.bus.error.emit(err)
                    finally:
                        if on_finish:
                            try:
                                on_finish()
                            except Exception:
                                pass

                self.ui_invoke(apply_err)
            else:
                def apply_ok():
                    try:
                        done(res)
                    finally:
                        try:
                            self.clear_activity()
                        finally:
                            if on_finish:
                                try:
                                    on_finish()
                                except Exception:
                                    pass

                self.ui_invoke(apply_ok)

        threading.Thread(target=runner, daemon=True).start()

    # ---------- Delayed execution (Quick Post) ----------
    def schedule_or_run(
            self,
            delay_min: int,
            activity: str,
            task_fn: Callable[[], Any],
            done_fn: Callable[[Any], None],
            on_finish: Optional[Callable[[], None]] = None,
    ):
        """
        If delay_min <= 0, run immediately via run_bg.
        If delay_min > 0, schedule via QTimer.singleShot (Qt-safe).
        """
        if delay_min <= 0:
            self.run_bg(activity, task_fn, done_fn, on_finish=on_finish)
            return

        # Show scheduling in "current action" log
        self._start_action_log("Scheduling…")
        self.set_activity("Scheduling…")

        delay_ms = int(delay_min) * 60 * 1000

        # Friendly feedback
        self.bus.log_line.emit(f"Scheduled: {activity} in {delay_min} min.")
        self.set_activity(f"Scheduled in {delay_min} min")

        def fire():
            self.run_bg(activity, task_fn, done_fn, on_finish=on_finish)

        QTimer.singleShot(delay_ms, fire)

    # ---------- Slack-style split button helper ----------
    def _make_send_split_button(
            self,
            text: str,
            send_now_cb: Callable[[], None],
            send_later_cb: Callable[[int], None],
    ) -> QToolButton:
        """
        Slack-style: main click sends immediately; arrow opens delay menu.
        """
        btn = QToolButton()
        btn.setText(text)
        btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        btn.setPopupMode(QToolButton.MenuButtonPopup)

        menu = QMenu(btn)

        def add_delay(label: str, minutes: int):
            act = menu.addAction(label)
            act.triggered.connect(lambda _=False, m=minutes: send_later_cb(m))

        add_delay("Send in 5 min", 5)
        add_delay("Send in 10 min", 10)
        add_delay("Send in 20 min", 20)
        add_delay("Send in 30 min", 30)
        menu.addSeparator()

        custom = menu.addAction("Custom…")

        def on_custom():
            m, ok = QInputDialog.getInt(
                self,
                "Schedule Send",
                "Send in how many minutes?",
                30,  # default
                1,  # min
                24 * 60  # max (1 day)
            )
            if ok and m > 0:
                send_later_cb(int(m))

        custom.triggered.connect(on_custom)

        btn.setMenu(menu)
        btn.clicked.connect(send_now_cb)
        return btn

    # ----- Build UI -----
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        # Top bar
        top = QHBoxLayout()
        root_layout.addLayout(top)

        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText("Paste Moltbook API key (moltbook_...)")
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setMinimumWidth(420)

        self.btn_show = QPushButton("Show")
        self.btn_connect = QPushButton("Connect")
        self.btn_save = QPushButton("Save Key")

        self.status_label = QLabel("Not connected.")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.activity_label = QLabel("")
        self.activity_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.dark_mode_chk = QCheckBox("Dark")
        self.dark_mode_chk.setChecked(True)

        top.addWidget(QLabel("API Key:"))
        top.addWidget(self.api_key_input)
        top.addWidget(self.btn_show)
        top.addWidget(self.btn_connect)
        top.addWidget(self.btn_save)
        top.addSpacing(10)
        top.addWidget(self.status_label, 1)
        top.addWidget(self.activity_label)

        self.btn_show.clicked.connect(self.on_toggle_key)
        self.btn_save.clicked.connect(self.on_save_key)
        self.btn_connect.clicked.connect(self.on_connect_clicked)

        # Splitter (LEFT vs RIGHT) — larger draggable range:
        #  - explicit handle width
        #  - explicit minimum widths (so the handle can travel further without “hitting” a hard min too early)
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setHandleWidth(8)
        root_layout.addWidget(self.main_splitter, 1)

        left = QWidget()
        left.setMinimumWidth(320)   # allow right to become thinner
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        self.tabs_left = QTabWidget()
        left_layout.addWidget(self.tabs_left, 1)
        self.main_splitter.addWidget(left)

        right = QWidget()
        right.setMinimumWidth(260)  # right can be thinner, but not “disappear”
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        self.main_splitter.addWidget(right)

        self.main_splitter.setStretchFactor(0, 2)
        self.main_splitter.setStretchFactor(1, 1)

        # Initial sizes (more room to drag; right starts bigger but can go thinner)
        self.main_splitter.setSizes([880, 500])

        # -------- Feed tab
        feed_tab = QWidget()
        feed_layout = QVBoxLayout(feed_tab)
        feed_layout.setSpacing(10)

        feed_controls = QGroupBox("Feed")
        fc = QGridLayout(feed_controls)

        self.feed_type = QComboBox()
        self.feed_type.addItems(["global", "personalized", "submolt"])
        self.feed_sort = QComboBox()
        self.feed_sort.addItems(["hot", "new", "top", "rising"])
        self.feed_limit = QSpinBox()
        self.feed_limit.setRange(1, 50)
        self.feed_limit.setValue(25)

        self.feed_submolt = QLineEdit()
        self.feed_submolt.setPlaceholderText("Submolt name (e.g. general)")

        self.btn_refresh_feed = QPushButton("Refresh")
        self.btn_refresh_feed.clicked.connect(self.on_refresh_feed_clicked)

        # Quick-post collapse (restored)
        self.btn_toggle_quickpost = QPushButton("Hide Quick Post")
        self.btn_toggle_quickpost.clicked.connect(self.on_toggle_quick_post)

        fc.addWidget(QLabel("Type"), 0, 0)
        fc.addWidget(self.feed_type, 0, 1)
        fc.addWidget(QLabel("Sort"), 0, 2)
        fc.addWidget(self.feed_sort, 0, 3)
        fc.addWidget(QLabel("Limit"), 0, 4)
        fc.addWidget(self.feed_limit, 0, 5)
        fc.addWidget(QLabel("Submolt"), 1, 0)
        fc.addWidget(self.feed_submolt, 1, 1, 1, 3)
        fc.addWidget(self.btn_refresh_feed, 1, 4, 1, 1)
        fc.addWidget(self.btn_toggle_quickpost, 1, 5, 1, 1)

        feed_layout.addWidget(feed_controls)

        # Feed vertical splitter (Feed list + Quick Post) — draggable + collapsible
        self.feed_splitter = QSplitter(Qt.Vertical)
        self.feed_splitter.setHandleWidth(8)
        feed_layout.addWidget(self.feed_splitter, 1)

        self.feed_list = QListWidget()
        self.feed_list.itemSelectionChanged.connect(self.on_select_feed_item)
        self.feed_splitter.addWidget(self.feed_list)

        self.quick_post_box = QGroupBox("Quick Post")
        qp = QGridLayout(self.quick_post_box)

        self.new_post_submolt = QLineEdit()
        self.new_post_submolt.setText("general")
        self.new_post_title = QLineEdit()
        self.new_post_title.setPlaceholderText("Title")
        self.new_post_url = QLineEdit()
        self.new_post_url.setPlaceholderText("URL (optional for link post)")

        self.new_post_content = QPlainTextEdit()
        self.new_post_content.setPlaceholderText("Content (for text post)")
        # Make Quick Post panel SHORT
        self.new_post_content.setFixedHeight(88)

        # Slack-style split buttons: click = send now, arrow = schedule
        self.btn_create_text = self._make_send_split_button(
            "Create Text Post",
            send_now_cb=self.on_create_text_post,
            send_later_cb=self.on_create_text_post_scheduled,
        )
        self.btn_create_link = self._make_send_split_button(
            "Create Link Post",
            send_now_cb=self.on_create_link_post,
            send_later_cb=self.on_create_link_post_scheduled,
        )

        qp.addWidget(QLabel("Submolt"), 0, 0)
        qp.addWidget(self.new_post_submolt, 0, 1)
        qp.addWidget(QLabel("Title"), 1, 0)
        qp.addWidget(self.new_post_title, 1, 1)
        qp.addWidget(QLabel("URL"), 2, 0)
        qp.addWidget(self.new_post_url, 2, 1)
        qp.addWidget(QLabel("Content"), 3, 0, alignment=Qt.AlignTop)
        qp.addWidget(self.new_post_content, 3, 1)
        qp.addWidget(self.btn_create_link, 4, 0)
        qp.addWidget(self.btn_create_text, 4, 1)

        self.feed_splitter.addWidget(self.quick_post_box)
        # default: mostly feed list, small quick post
        self.feed_splitter.setSizes([760, 160])

        self.tabs_left.addTab(feed_tab, "Feed")

        # -------- Search tab
        search_tab = QWidget()
        s_layout = QVBoxLayout(search_tab)

        search_controls = QGroupBox("Semantic Search")
        sc = QGridLayout(search_controls)

        self.search_q = QLineEdit()
        self.search_q.setPlaceholderText("Search query (natural language works best)")
        self.search_type = QComboBox()
        self.search_type.addItems(["all", "posts", "comments"])
        self.search_limit = QSpinBox()
        self.search_limit.setRange(1, 50)
        self.search_limit.setValue(20)
        self.btn_search = QPushButton("Search")
        self.btn_search.clicked.connect(self.on_search_clicked)

        sc.addWidget(QLabel("Query"), 0, 0)
        sc.addWidget(self.search_q, 0, 1, 1, 3)
        sc.addWidget(QLabel("Type"), 1, 0)
        sc.addWidget(self.search_type, 1, 1)
        sc.addWidget(QLabel("Limit"), 1, 2)
        sc.addWidget(self.search_limit, 1, 3)
        sc.addWidget(self.btn_search, 2, 0, 1, 4)

        s_layout.addWidget(search_controls)

        self.search_list = QListWidget()
        self.search_list.itemSelectionChanged.connect(self.on_select_search_item)
        s_layout.addWidget(self.search_list, 1)

        self.tabs_left.addTab(search_tab, "Search")

        # -------- Submolts tab
        sub_tab = QWidget()
        sub_layout = QVBoxLayout(sub_tab)

        sub_controls = QGroupBox("Submolts")
        subc = QGridLayout(sub_controls)

        self.btn_submolts_refresh = QPushButton("List Submolts")
        self.btn_submolts_refresh.clicked.connect(self.on_list_submolts)

        self.submolt_pick = QComboBox()
        self.submolt_pick.setEditable(True)
        self.btn_submolt_info = QPushButton("Load Info")
        self.btn_submolt_info.clicked.connect(self.on_load_submolt_info)
        self.btn_submolt_sub = QPushButton("Subscribe")
        self.btn_submolt_unsub = QPushButton("Unsubscribe")
        self.btn_submolt_sub.clicked.connect(self.on_subscribe_submolt)
        self.btn_submolt_unsub.clicked.connect(self.on_unsubscribe_submolt)

        subc.addWidget(self.btn_submolts_refresh, 0, 0, 1, 2)
        subc.addWidget(QLabel("Submolt"), 1, 0)
        subc.addWidget(self.submolt_pick, 1, 1, 1, 3)
        subc.addWidget(self.btn_submolt_info, 2, 0)
        subc.addWidget(self.btn_submolt_sub, 2, 1)
        subc.addWidget(self.btn_submolt_unsub, 2, 2)

        self.create_sub_name = QLineEdit()
        self.create_sub_name.setPlaceholderText("name (no 'm/')")
        self.create_sub_display = QLineEdit()
        self.create_sub_display.setPlaceholderText("display_name")
        self.create_sub_desc = QLineEdit()
        self.create_sub_desc.setPlaceholderText("description")
        self.btn_create_submolt = QPushButton("Create Submolt")
        self.btn_create_submolt.clicked.connect(self.on_create_submolt)

        subc.addWidget(QLabel("Create"), 3, 0)
        subc.addWidget(self.create_sub_name, 3, 1)
        subc.addWidget(self.create_sub_display, 3, 2)
        subc.addWidget(self.create_sub_desc, 3, 3)
        subc.addWidget(self.btn_create_submolt, 4, 0, 1, 4)

        sub_layout.addWidget(sub_controls)

        self.submolts_list = QListWidget()
        self.submolts_list.itemSelectionChanged.connect(self.on_select_submolt_from_list)
        sub_layout.addWidget(self.submolts_list, 1)

        self.tabs_left.addTab(sub_tab, "Submolts")

        # -------- Agents tab
        agents_tab = QWidget()
        a_layout = QVBoxLayout(agents_tab)

        agent_controls = QGroupBox("Agents / Following")
        ac = QGridLayout(agent_controls)

        self.agent_lookup_name = QLineEdit()
        self.agent_lookup_name.setPlaceholderText("Molty name")
        self.btn_agent_profile = QPushButton("Load Profile")
        self.btn_agent_follow = QPushButton("Follow")
        self.btn_agent_unfollow = QPushButton("Unfollow")
        self.btn_agent_profile.clicked.connect(self.on_agent_profile)
        self.btn_agent_follow.clicked.connect(self.on_agent_follow)
        self.btn_agent_unfollow.clicked.connect(self.on_agent_unfollow)

        ac.addWidget(QLabel("Molty"), 0, 0)
        ac.addWidget(self.agent_lookup_name, 0, 1, 1, 3)
        ac.addWidget(self.btn_agent_profile, 1, 0)
        ac.addWidget(self.btn_agent_follow, 1, 1)
        ac.addWidget(self.btn_agent_unfollow, 1, 2)

        self.my_desc = QLineEdit()
        self.my_desc.setPlaceholderText("Update my description")
        self.my_metadata = QLineEdit()
        self.my_metadata.setPlaceholderText('metadata JSON (optional),')
        self.btn_update_me = QPushButton("Update My Profile")
        self.btn_update_me.clicked.connect(self.on_update_me)

        self.btn_upload_avatar = QPushButton("Upload My Avatar…")
        self.btn_remove_avatar = QPushButton("Remove My Avatar")
        self.btn_upload_avatar.clicked.connect(self.on_upload_my_avatar)
        self.btn_remove_avatar.clicked.connect(self.on_remove_my_avatar)

        ac.addWidget(QLabel("Me"), 2, 0)
        ac.addWidget(self.my_desc, 2, 1, 1, 3)
        ac.addWidget(self.my_metadata, 3, 1, 1, 3)
        ac.addWidget(self.btn_update_me, 4, 0, 1, 2)
        ac.addWidget(self.btn_upload_avatar, 4, 2)
        ac.addWidget(self.btn_remove_avatar, 4, 3)

        a_layout.addWidget(agent_controls)

        self.agents_output = QPlainTextEdit()
        self.agents_output.setReadOnly(True)
        a_layout.addWidget(self.agents_output, 1)

        self.tabs_left.addTab(agents_tab, "Agents")

        # -------- Moderation tab
        mod_tab = QWidget()
        m_layout = QVBoxLayout(mod_tab)

        mod_controls = QGroupBox("Moderation / Submolt Settings")
        mc = QGridLayout(mod_controls)

        self.mod_submolt = QLineEdit()
        self.mod_submolt.setPlaceholderText("Submolt name (e.g. general)")
        self.mod_desc = QLineEdit()
        self.mod_desc.setPlaceholderText("description (optional)")
        self.mod_banner_color = QLineEdit()
        self.mod_banner_color.setPlaceholderText("banner_color (optional) e.g. #1a1a2e")
        self.mod_theme_color = QLineEdit()
        self.mod_theme_color.setPlaceholderText("theme_color (optional) e.g. #ff4500")
        self.btn_mod_patch = QPushButton("Update Settings")
        self.btn_mod_patch.clicked.connect(self.on_update_submolt_settings)

        self.btn_mod_avatar = QPushButton("Upload Submolt Avatar…")
        self.btn_mod_banner = QPushButton("Upload Submolt Banner…")
        self.btn_mod_avatar.clicked.connect(lambda: self.on_upload_submolt_media("avatar"))
        self.btn_mod_banner.clicked.connect(lambda: self.on_upload_submolt_media("banner"))

        self.mod_agent = QLineEdit()
        self.mod_agent.setPlaceholderText("Agent name to add/remove")
        self.btn_add_mod = QPushButton("Add Moderator")
        self.btn_remove_mod = QPushButton("Remove Moderator")
        self.btn_list_mods = QPushButton("List Moderators")
        self.btn_add_mod.clicked.connect(self.on_add_moderator)
        self.btn_remove_mod.clicked.connect(self.on_remove_moderator)
        self.btn_list_mods.clicked.connect(self.on_list_moderators)

        mc.addWidget(QLabel("Submolt"), 0, 0)
        mc.addWidget(self.mod_submolt, 0, 1, 1, 3)
        mc.addWidget(QLabel("Desc"), 1, 0)
        mc.addWidget(self.mod_desc, 1, 1, 1, 3)
        mc.addWidget(QLabel("Banner"), 2, 0)
        mc.addWidget(self.mod_banner_color, 2, 1)
        mc.addWidget(QLabel("Theme"), 2, 2)
        mc.addWidget(self.mod_theme_color, 2, 3)
        mc.addWidget(self.btn_mod_patch, 3, 0, 1, 2)
        mc.addWidget(self.btn_mod_avatar, 3, 2)
        mc.addWidget(self.btn_mod_banner, 3, 3)

        mc.addWidget(QLabel("Mod Agent"), 4, 0)
        mc.addWidget(self.mod_agent, 4, 1, 1, 2)
        mc.addWidget(self.btn_add_mod, 5, 0)
        mc.addWidget(self.btn_remove_mod, 5, 1)
        mc.addWidget(self.btn_list_mods, 5, 2)

        m_layout.addWidget(mod_controls)

        self.mod_output = QPlainTextEdit()
        self.mod_output.setReadOnly(True)
        m_layout.addWidget(self.mod_output, 1)

        self.tabs_left.addTab(mod_tab, "Moderation")

        # -------- Log tab
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)

        log_box = QGroupBox("Log")
        lb = QVBoxLayout(log_box)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(2000)

        lb.addWidget(self.log_output)
        log_layout.addWidget(log_box, 1)

        self.tabs_left.addTab(log_tab, "Log")

        # -------- Right side (post + comments)
        post_box = QGroupBox("Post")
        pb = QVBoxLayout(post_box)
        pb.setSpacing(6)

        self.post_title = QLabel("Select a post…")
        self.post_title.setStyleSheet("font-size: 16px; font-weight: 700;")
        pb.addWidget(self.post_title)

        goto_row = QHBoxLayout()
        self.goto_post_id = QLineEdit()
        self.goto_post_id.setPlaceholderText("Post ID")
        self.goto_post_id.setMinimumWidth(420)
        self.goto_post_id.returnPressed.connect(self.on_goto_post_clicked)
        self.btn_goto_post = QPushButton("Go")
        self.btn_goto_post.clicked.connect(self.on_goto_post_clicked)
        goto_row.addWidget(self.goto_post_id, 1)
        goto_row.addWidget(self.btn_goto_post)
        pb.addLayout(goto_row)

        post_actions = QHBoxLayout()
        self.btn_post_reload = QPushButton("Reload")
        self.btn_post_upvote = QPushButton("Upvote")
        self.btn_post_downvote = QPushButton("Downvote")
        self.btn_post_delete = QPushButton("Delete (mine)")
        self.btn_post_pin = QPushButton("Pin")
        self.btn_post_unpin = QPushButton("Unpin")
        self.btn_post_reload.clicked.connect(self.on_reload_post)
        self.btn_post_upvote.clicked.connect(self.on_upvote_post)
        self.btn_post_downvote.clicked.connect(self.on_downvote_post)
        self.btn_post_delete.clicked.connect(self.on_delete_post)
        self.btn_post_pin.clicked.connect(self.on_pin_post)
        self.btn_post_unpin.clicked.connect(self.on_unpin_post)

        for w in [self.btn_post_reload, self.btn_post_upvote, self.btn_post_downvote, self.btn_post_delete, self.btn_post_pin, self.btn_post_unpin]:
            post_actions.addWidget(w)

        pb.addLayout(post_actions)

        self.post_body = QPlainTextEdit()
        self.post_body.setReadOnly(True)
        pb.addWidget(self.post_body, 1)

        right_layout.addWidget(post_box, 7)

        comments_box = QGroupBox("Comments")
        cb = QVBoxLayout(comments_box)

        cbar = QHBoxLayout()
        self.comments_sort = QComboBox()
        self.comments_sort.addItems(["top", "new", "controversial"])
        self.btn_load_comments = QPushButton("Load")
        self.btn_probe_comments = QPushButton("Probe API")
        self.btn_load_comments.clicked.connect(self.on_load_comments)
        self.btn_probe_comments.clicked.connect(self.on_probe_comments_api)

        self.upvote_comment_id = QLineEdit()
        self.upvote_comment_id.setPlaceholderText("comment_id to upvote")
        self.btn_upvote_comment = QPushButton("Upvote Comment")
        self.btn_upvote_comment.clicked.connect(self.on_upvote_comment)

        cbar.addWidget(QLabel("Sort"))
        cbar.addWidget(self.comments_sort)
        cbar.addWidget(self.btn_load_comments)
        cbar.addWidget(self.btn_probe_comments)
        cbar.addSpacing(8)
        cbar.addWidget(self.upvote_comment_id, 1)
        cbar.addWidget(self.btn_upvote_comment)
        cb.addLayout(cbar)

        self.comments_list = QListWidget()
        self.comments_list.itemSelectionChanged.connect(self.on_select_comment)
        cb.addWidget(self.comments_list, 2)

        composer = QGroupBox("Write a comment / reply")
        comp = QGridLayout(composer)

        self.reply_parent_id = QLineEdit()
        self.reply_parent_id.setPlaceholderText("parent_id (optional)")
        self.btn_clear_reply = QPushButton("Clear reply target")
        self.btn_clear_reply.clicked.connect(lambda: self.reply_parent_id.setText(""))

        self.comment_text = QPlainTextEdit()
        self.comment_text.setPlaceholderText("Write comment…")
        self.btn_post_comment = QPushButton("Post Comment")
        self.btn_post_comment.clicked.connect(self.on_post_comment)

        comp.addWidget(QLabel("Reply to"), 0, 0)
        comp.addWidget(self.reply_parent_id, 0, 1)
        comp.addWidget(self.btn_clear_reply, 0, 2)
        comp.addWidget(self.comment_text, 1, 0, 1, 3)
        comp.addWidget(self.btn_post_comment, 2, 0, 1, 3)

        cb.addWidget(composer, 1)

        right_layout.addWidget(comments_box, 3)

    # ---------- Theme ----------
    def apply_glass_dark_theme(self):
        QApplication.setStyle("Fusion")

        pal = QPalette()
        pal.setColor(QPalette.Window, QColor(14, 14, 18))
        pal.setColor(QPalette.WindowText, QColor(235, 235, 242))
        pal.setColor(QPalette.Base, QColor(16, 16, 20))
        pal.setColor(QPalette.AlternateBase, QColor(22, 22, 28))
        pal.setColor(QPalette.ToolTipBase, QColor(30, 30, 36))
        pal.setColor(QPalette.ToolTipText, QColor(240, 240, 245))
        pal.setColor(QPalette.Text, QColor(235, 235, 242))
        pal.setColor(QPalette.Button, QColor(30, 30, 40))
        pal.setColor(QPalette.ButtonText, QColor(235, 235, 242))
        pal.setColor(QPalette.Highlight, QColor(90, 140, 255))
        pal.setColor(QPalette.HighlightedText, QColor(10, 10, 12))
        QApplication.setPalette(pal)

        # IMPORTANT:
        # - We keep splitter handles subtle (no “thick ugly bar”), but still draggable.
        self.setStyleSheet(
            """
            QWidget { color: rgba(235,235,242,0.95); font-size: 13px; }
            QGroupBox {
                font-weight: 700;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 16px;
                margin-top: 10px;
                background: rgba(255,255,255,0.04);
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 8px;
                color: rgba(235,235,242,0.90);
            }
            QTabWidget::pane {
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 16px;
                background: rgba(255,255,255,0.03);
            }
            QTabBar::tab {
                background: rgba(255,255,255,0.04);
                border: 1px solid rgba(255,255,255,0.08);
                padding: 10px 14px;
                margin-right: 8px;
                border-radius: 12px;
            }
            QTabBar::tab:selected {
                background: rgba(120,170,255,0.18);
                border: 1px solid rgba(120,170,255,0.35);
            }
            QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
                background: rgba(10,10,14,0.55);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
                padding: 8px 10px;
                selection-background-color: rgba(90,140,255,0.65);
            }
            QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
                border: 1px solid rgba(120,170,255,0.55);
                background: rgba(10,10,14,0.62);
            }
            QPushButton {
                background: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 12px;
                padding: 9px 12px;
                font-weight: 650;
            }
            QPushButton:hover {
                background: rgba(255,255,255,0.11);
                border: 1px solid rgba(255,255,255,0.18);
            }
            QPushButton:pressed {
                background: rgba(90,140,255,0.22);
                border: 1px solid rgba(90,140,255,0.35);
            }
            QListWidget {
                background: rgba(10,10,14,0.35);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 16px;
                padding: 6px;
            }
            QListWidget::item {
                padding: 10px;
                border-radius: 12px;
            }
            QListWidget::item:selected {
                background: rgba(90,140,255,0.22);
                border: 1px solid rgba(90,140,255,0.25);
            }

            /* Subtle splitter handle (still draggable) */
            QSplitter::handle {
                background: rgba(255,255,255,0.03);
                border: 0px;
            }
            QSplitter::handle:hover {
                background: rgba(120,170,255,0.10);
            }
            """
        )

    # ---------- Quick Post collapse ----------
    @safe_slot
    def on_toggle_quick_post(self):
        if not self._quickpost_collapsed:
            # Save sizes so we can restore
            self._quickpost_prev_sizes = self.feed_splitter.sizes()
            self.quick_post_box.setVisible(False)
            self._quickpost_collapsed = True
            self.btn_toggle_quickpost.setText("Show Quick Post")
            # Give everything to feed list
            self.feed_splitter.setSizes([1000, 0])
        else:
            self.quick_post_box.setVisible(True)
            self._quickpost_collapsed = False
            self.btn_toggle_quickpost.setText("Hide Quick Post")
            if self._quickpost_prev_sizes and len(self._quickpost_prev_sizes) == 2:
                self.feed_splitter.setSizes(self._quickpost_prev_sizes)
            else:
                self.feed_splitter.setSizes([760, 160])

    # ---------- Logging ----------
    def _append_log_ui(self, msg: str):
        self.log_output.appendPlainText(msg)

    # ---------- Credentials ----------
    def _load_saved_key(self):
        api_key = (self.creds.get("api_key") or "").strip()
        if api_key:
            self.api_key_input.setText(api_key)
            self.client.set_api_key(api_key)
            self.status_label.setText("API key loaded. Click Connect.")

    def require_key(self):
        api_key = self.api_key_input.text().strip()
        if not api_key:
            raise ValueError("Paste your Moltbook API key first.")
        self.client.set_api_key(api_key)

    # ---------- Formatting ----------
    def pretty_post_line(self, p: dict) -> str:
        pid = p.get("id", "??")
        title = (p.get("title") or "").strip().replace("\n", " ")
        author = (p.get("author") or {}).get("name") if isinstance(p.get("author"), dict) else p.get("author")
        submolt = (p.get("submolt") or {}).get("name") if isinstance(p.get("submolt"), dict) else p.get("submolt")
        ups = p.get("upvotes", 0)
        dns = p.get("downvotes", 0)
        return f"[m/{submolt}] (+{ups}/-{dns}) {title} — {author}   ({pid})"

    def pretty_search_line(self, r: dict) -> str:
        rtype = r.get("type")
        author = (r.get("author") or {}).get("name") if isinstance(r.get("author"), dict) else r.get("author")
        sim = r.get("similarity", None)

        if rtype == "post":
            title = (r.get("title") or "(no title)").strip().replace("\n", " ")
            sub = (r.get("submolt") or {}).get("name") if isinstance(r.get("submolt"), dict) else (r.get("submolt") or "")
            pid = r.get("post_id")
            s = f"[POST m/{sub}] {title} — {author} (post_id:{pid or 'MISSING'})"
        else:
            txt = (r.get("content") or "").strip().replace("\n", " ")
            if len(txt) > 140:
                txt = txt[:140] + "…"
            pid = r.get("post_id")
            cid = r.get("id")
            s = f"[COMMENT] {author}: {txt} (post_id:{pid or 'MISSING'} comment_id:{cid})"

        if sim is not None:
            try:
                s = f"{s}  (sim:{float(sim):.2f})"
            except Exception:
                pass
        return s

    def pretty_comment_line(self, c: dict) -> str:
        cid = c.get("id", "??")
        author = (c.get("author") or {}).get("name") if isinstance(c.get("author"), dict) else c.get("author")
        ups = c.get("upvotes", 0)
        dns = c.get("downvotes", 0)
        txt = (c.get("content") or "").strip().replace("\n", " ")
        if len(txt) > 180:
            txt = txt[:180] + "…"
        parent = c.get("parent_id") or ""
        s = f"(+{ups}/-{dns}) {author}: {txt}  [id:{cid}]"
        if parent:
            s += f" reply_to:{parent}"
        return s

    # ---------- Top bar actions ----------
    @safe_slot
    def on_toggle_key(self):
        if self.api_key_input.echoMode() == QLineEdit.Password:
            self.api_key_input.setEchoMode(QLineEdit.Normal)
            self.btn_show.setText("Hide")
        else:
            self.api_key_input.setEchoMode(QLineEdit.Password)
            self.btn_show.setText("Show")

    @safe_slot
    def on_save_key(self):
        self.require_key()
        save_creds(self.client.api_key, agent_name=self.agent_name_cached or "")
        self.status_label.setText(f"Saved key to {CRED_PATH}")

    @safe_slot
    def on_connect_clicked(self):
        # Disable connect while connecting (prevents double threads)
        self.btn_connect.setEnabled(False)
        self.status_label.setText("Connecting…")

        def task():
            self.require_key()
            me_resp = self.client.me()
            st_resp = self.client.status()

            me = parse_json(self.client, me_resp)
            st = parse_json(self.client, st_resp)

            if me_resp.status_code != 200:
                raise ValueError(me.get("error") or json.dumps(me, indent=2, ensure_ascii=False))

            agent_name = extract_agent_name(me)
            claim = st.get("status")
            if not claim and isinstance(st.get("data"), dict):
                claim = st["data"].get("status")

            return agent_name, claim

        def done(res):
            agent_name, claim = res
            msg = "Connected"
            if agent_name:
                msg += f" as {agent_name}"
                self.agent_name_cached = agent_name
            if claim:
                msg += f" (status: {claim})"
            self.status_label.setText(msg)

        # CRITICAL: always re-enable Connect, even on error (fixes “stuck connecting” UI hang)
        self.run_bg(
            "Connecting…",
            task,
            done,
            on_finish=lambda: self.btn_connect.setEnabled(True),
        )

    # ---------- Feed ----------
    @safe_slot
    def on_refresh_feed_clicked(self):
        def task():
            self.require_key()
            ftype = self.feed_type.currentText()
            sort = self.feed_sort.currentText()
            limit = int(self.feed_limit.value())
            submolt = normalize_submolt(self.feed_submolt.text())

            if ftype == "personalized":
                resp = self.client.personalized_feed(sort=sort, limit=limit)
            elif ftype == "submolt":
                if not submolt:
                    raise ValueError("Enter a submolt name for submolt feed.")
                safe_sort = sort if sort in ["hot", "new", "top"] else "new"
                resp = self.client.submolt_feed(submolt=submolt, sort=safe_sort, limit=limit)
            else:
                resp = self.client.feed_posts(sort=sort, limit=limit, submolt=submolt or None)

            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))

            posts = extract_posts_list(data)
            if posts is None:
                raise ValueError("Could not parse posts list:\n" + json.dumps(data, indent=2, ensure_ascii=False))
            return posts

        def done(posts):
            self.posts = posts
            self.feed_list.clear()
            for p in posts:
                self.feed_list.addItem(self.pretty_post_line(p))
            self.tabs_left.setCurrentIndex(0)

        self.run_bg("Loading feed…", task, done)

    @safe_slot
    def on_select_feed_item(self):
        items = self.feed_list.selectedItems()
        if not items:
            return
        idx = self.feed_list.row(items[0])
        if idx < 0 or idx >= len(self.posts):
            return
        pid = self.posts[idx].get("id")
        if not pid:
            return
        self.selected_post_id = pid
        self.load_post(pid, activity="Loading post…")

    # ---------- Search ----------
    @safe_slot
    def on_search_clicked(self):
        q = self.search_q.text().strip()
        if not q:
            QMessageBox.information(self, "Info", "Enter a search query.")
            return

        def task():
            self.require_key()
            type_ = self.search_type.currentText().strip() or "all"
            limit = int(self.search_limit.value())

            resp = self.client.semantic_search(q=q, type_=type_, limit=limit)
            data = parse_json(self.client, resp)

            if resp.status_code == 500 and type_ == "all":
                self.bus.log_line.emit("Search returned 500; retrying with type=posts…")
                resp2 = self.client.semantic_search(q=q, type_="posts", limit=limit)
                data = parse_json(self.client, resp2)
                resp = resp2

            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))

            results = extract_results_list(data)
            if results is None:
                raise ValueError("Could not parse search results:\n" + json.dumps(data, indent=2, ensure_ascii=False))
            return results

        def done(results):
            self.search_results = results
            self.search_list.clear()
            for r in results:
                self.search_list.addItem(self.pretty_search_line(r))
            self.tabs_left.setCurrentIndex(1)

        self.run_bg("Searching…", task, done)

    @safe_slot
    def on_select_search_item(self):
        items = self.search_list.selectedItems()
        if not items:
            return
        idx = self.search_list.row(items[0])
        if idx < 0 or idx >= len(self.search_results):
            return
        r = self.search_results[idx]
        rtype = r.get("type")

        if rtype == "post":
            pid = r.get("post_id")
            if not pid:
                raise ValueError("Search result missing post_id.")
            self.selected_post_id = pid
            self.load_post(pid, activity="Loading post…")

        elif rtype == "comment":
            pid = r.get("post_id")
            cid = r.get("id")
            if not pid:
                raise ValueError("Comment search result missing post_id.")
            self.selected_post_id = pid

            def after_loaded(_post):
                if cid:
                    self.reply_parent_id.setText(cid)
                    self.upvote_comment_id.setText(cid)
                    self.bus.log_line.emit(f"Selected comment result; reply target set to {cid}")
                self.bus.log_line.emit("Note: Not auto-loading comments (comments endpoint may return 405).")

            self.load_post(pid, activity="Loading parent post…", after_done=after_loaded)

        else:
            raise ValueError(f"Unknown search result type: {rtype}")

    # ---------- Post load/render ----------
    def load_post(self, post_id: str, activity: str = "Loading…", after_done: Optional[Callable[[Any], None]] = None):
        def task():
            self.require_key()
            resp = self.client.get_post(post_id)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            post = extract_post_obj(data)
            if post is None:
                raise ValueError("Could not parse post:\n" + json.dumps(data, indent=2, ensure_ascii=False))
            return post

        def done(post):
            self.render_post(post)
            if after_done:
                after_done(post)

        self.run_bg(activity, task, done)

    def render_post(self, p: dict):
        title = p.get("title") or "(no title)"
        author = (p.get("author") or {}).get("name") if isinstance(p.get("author"), dict) else p.get("author")
        submolt = (p.get("submolt") or {}).get("name") if isinstance(p.get("submolt"), dict) else p.get("submolt")
        ups = p.get("upvotes", 0)
        dns = p.get("downvotes", 0)
        url = p.get("url")
        content = p.get("content") or ""

        header = f"{title}\nby {author} in m/{submolt}   (+{ups}/-{dns})\npost_id: {p.get('id')}\n"
        if url:
            header += f"url: {url}\n"
        header += "\n"

        self.post_title.setText(title)
        self.post_body.setPlainText(header + content)

    # ---------- Post actions ----------
    @safe_slot
    def on_goto_post_clicked(self):
        pid = self.goto_post_id.text().strip()
        if not pid:
            raise ValueError("Paste a post_id first.")

        # Basic UUID sanity check (still allows valid UUID strings)
        if not re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", pid):
            raise ValueError("That doesn't look like a UUID post_id.")

        # IMPORTANT: update selection state so reload/upvote/etc works on this post too
        self.selected_post_id = pid
        # Give immediate visual feedback (so it never feels stuck)
        self.post_title.setText("Loading…")
        self.post_body.setPlainText(f"Loading post:\n{pid}")
        # This is what actually loads and renders the post into the post box
        self.load_post(pid, activity="Loading post…")

    @safe_slot
    def on_reload_post(self):
        if not self.selected_post_id:
            QMessageBox.information(self, "Info", "Select a post first.")
            return
        self.load_post(self.selected_post_id, activity="Reloading post…")

    @safe_slot
    def on_upvote_post(self):
        if not self.selected_post_id:
            QMessageBox.information(self, "Info", "Select a post first.")
            return

        pid = self.selected_post_id

        def task():
            self.require_key()
            resp = self.client.upvote_post(pid)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return True

        def done(_):
            self.load_post(pid, activity="Refreshing post…")

        self.run_bg("Upvoting…", task, done)

    @safe_slot
    def on_downvote_post(self):
        if not self.selected_post_id:
            QMessageBox.information(self, "Info", "Select a post first.")
            return

        pid = self.selected_post_id

        def task():
            self.require_key()
            resp = self.client.downvote_post(pid)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return True

        def done(_):
            self.load_post(pid, activity="Refreshing post…")

        self.run_bg("Downvoting…", task, done)

    @safe_slot
    def on_pin_post(self):
        if not self.selected_post_id:
            QMessageBox.information(self, "Info", "Select a post first.")
            return

        pid = self.selected_post_id

        def task():
            self.require_key()
            resp = self.client.pin_post(pid)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Pinned", json.dumps(data, indent=2, ensure_ascii=False))
            self.load_post(pid, activity="Refreshing post…")

        self.run_bg("Pinning…", task, done)

    @safe_slot
    def on_unpin_post(self):
        if not self.selected_post_id:
            QMessageBox.information(self, "Info", "Select a post first.")
            return

        pid = self.selected_post_id

        def task():
            self.require_key()
            resp = self.client.unpin_post(pid)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Unpinned", json.dumps(data, indent=2, ensure_ascii=False))
            self.load_post(pid, activity="Refreshing post…")

        self.run_bg("Unpinning…", task, done)

    @safe_slot
    def on_delete_post(self):
        if not self.selected_post_id:
            QMessageBox.information(self, "Info", "Select a post first.")
            return
        if QMessageBox.question(self, "Confirm", "Delete this post? (Only works if it's yours)") != QMessageBox.Yes:
            return

        pid = self.selected_post_id

        def task():
            self.require_key()
            del_resp = self.client.delete_post(pid)
            del_data = parse_json(self.client, del_resp)
            if del_resp.status_code != 200:
                raise ValueError(del_data.get("error") or json.dumps(del_data, indent=2, ensure_ascii=False))

            verify_resp = self.client.get_post(pid)
            verify_data = parse_json(self.client, verify_resp)
            return del_data, verify_resp.status_code, verify_data

        def done(res):
            del_data, v_status, v_data = res

            self.selected_post_id = None
            self.post_title.setText("Select a post…")
            self.post_body.setPlainText("")
            self.comments_list.clear()
            self.comments = []

            self.on_refresh_feed_clicked()

            msg = "Delete API returned HTTP 200.\n\n"
            msg += "Delete response:\n" + json.dumps(del_data, indent=2, ensure_ascii=False) + "\n\n"
            msg += f"Verify GET /posts/{pid} returned HTTP {v_status}.\n"
            if v_status == 200:
                msg += (
                    "\n⚠️ Post still fetchable via API. That can mean soft-delete/caching or a backend bug.\n"
                    "Try refreshing feed again in ~10–30s.\n"
                )
            else:
                msg += "\n✅ Post not fetchable (likely deleted)."
            QMessageBox.information(self, "Delete Result", msg)

        self.run_bg("Deleting…", task, done)

    # ---------- Create posts ----------
    @safe_slot
    def on_create_text_post(self):
        def task():
            self.require_key()
            submolt = normalize_submolt(self.new_post_submolt.text())
            title = self.new_post_title.text().strip()
            content = self.new_post_content.toPlainText().strip()
            if not submolt or not title or not content:
                raise ValueError("Need submolt, title, and content.")
            resp = self.client.create_post(submolt=submolt, title=title, content=content)
            data = parse_json(self.client, resp)
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(
                self,
                "Posted",
                "Post created! (30-min cooldown may apply.)\n\n" + json.dumps(data, indent=2, ensure_ascii=False),
            )
            self.on_refresh_feed_clicked()

        self.run_bg("Creating post…", task, done)

    @safe_slot
    def on_create_link_post(self):
        def task():
            self.require_key()
            submolt = normalize_submolt(self.new_post_submolt.text())
            title = self.new_post_title.text().strip()
            url = self.new_post_url.text().strip()
            if not submolt or not title or not url:
                raise ValueError("Need submolt, title, and url.")
            resp = self.client.create_post(submolt=submolt, title=title, url=url)
            data = parse_json(self.client, resp)
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(
                self,
                "Posted",
                "Link post created! (30-min cooldown may apply.)\n\n" + json.dumps(data, indent=2, ensure_ascii=False),
            )
            self.on_refresh_feed_clicked()

        self.run_bg("Creating link post…", task, done)

    # ---------- Scheduled Quick Post ----------
    @safe_slot
    def on_create_text_post_scheduled(self, delay_min: int):
        def task():
            self.require_key()
            submolt = normalize_submolt(self.new_post_submolt.text())
            title = self.new_post_title.text().strip()
            content = self.new_post_content.toPlainText().strip()
            if not submolt or not title or not content:
                raise ValueError("Need submolt, title, and content.")
            resp = self.client.create_post(submolt=submolt, title=title, content=content)
            data = parse_json(self.client, resp)
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(
                self,
                "Posted",
                f"Post created! (Scheduled +{delay_min} min)\n\n" + json.dumps(data, indent=2, ensure_ascii=False),
            )
            self.on_refresh_feed_clicked()

        self.schedule_or_run(delay_min, "Creating post…", task, done)

    @safe_slot
    def on_create_link_post_scheduled(self, delay_min: int):
        def task():
            self.require_key()
            submolt = normalize_submolt(self.new_post_submolt.text())
            title = self.new_post_title.text().strip()
            url = self.new_post_url.text().strip()
            if not submolt or not title or not url:
                raise ValueError("Need submolt, title, and url.")
            resp = self.client.create_post(submolt=submolt, title=title, url=url)
            data = parse_json(self.client, resp)
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(
                self,
                "Posted",
                f"Link post created! (Scheduled +{delay_min} min)\n\n" + json.dumps(data, indent=2, ensure_ascii=False),
            )
            self.on_refresh_feed_clicked()

        self.schedule_or_run(delay_min, "Creating link post…", task, done)

    # ---------- Comments ----------
    @safe_slot
    def on_load_comments(self):
        if not self.selected_post_id:
            QMessageBox.information(self, "Info", "Select a post first.")
            return

        pid = self.selected_post_id
        sort = self.comments_sort.currentText()

        def task():
            self.require_key()
            resp = self.client.get_comments(pid, sort=sort)
            data = parse_json(self.client, resp)

            if resp.status_code == 405:
                return {"_status": 405, "_raw": data}

            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))

            comments = extract_comments_list(data)
            if comments is None:
                raise ValueError("Could not parse comments list:\n" + json.dumps(data, indent=2, ensure_ascii=False))
            return {"_status": 200, "comments": comments}

        def done(res):
            if res["_status"] == 405:
                QMessageBox.information(
                    self,
                    "Comments unavailable",
                    "Server returned HTTP 405 for GET comments.\n"
                    "You can still POST comments/replies.\n\n"
                    + json.dumps(res["_raw"], indent=2, ensure_ascii=False),
                )
                return

            self.comments = res["comments"]
            self.comments_list.clear()
            for c in self.comments:
                self.comments_list.addItem(self.pretty_comment_line(c))

        self.run_bg("Loading comments…", task, done)

    @safe_slot
    def on_select_comment(self):
        items = self.comments_list.selectedItems()
        if not items:
            return
        idx = self.comments_list.row(items[0])
        if idx < 0 or idx >= len(self.comments):
            return
        cid = self.comments[idx].get("id")
        if cid:
            self.reply_parent_id.setText(cid)

    @safe_slot
    def on_post_comment(self):
        if not self.selected_post_id:
            QMessageBox.information(self, "Info", "Select a post first.")
            return

        pid = self.selected_post_id
        content = self.comment_text.toPlainText().strip()
        parent = self.reply_parent_id.text().strip() or None

        if not content:
            raise ValueError("Write a comment first.")

        def task():
            self.require_key()
            self.bus.log_line.emit(f"POST comment: post_id={pid} parent_id={parent}")
            resp = self.client.add_comment(pid, content=content, parent_id=parent)
            data = parse_json(self.client, resp)
            self.bus.log_line.emit(f"Comment response HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)}")
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            self.comment_text.setPlainText("")
            QMessageBox.information(self, "Success", "Comment posted!\n\n" + json.dumps(data, indent=2, ensure_ascii=False))
            self.comments_sort.setCurrentText("new")
            self.on_load_comments()

        self.run_bg("Posting comment…", task, done)

    @safe_slot
    def on_upvote_comment(self):
        cid = self.upvote_comment_id.text().strip()
        if not cid:
            raise ValueError("Paste a comment id to upvote.")

        def task():
            self.require_key()
            resp = self.client.upvote_comment(cid)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Upvoted", json.dumps(data, indent=2, ensure_ascii=False))
            if self.selected_post_id:
                self.on_load_comments()

        self.run_bg("Upvoting comment…", task, done)

    @safe_slot
    def on_probe_comments_api(self):
        if not self.selected_post_id:
            QMessageBox.information(self, "Info", "Select a post first.")
            return
        pid = self.selected_post_id

        def task():
            self.require_key()
            candidates = [
                ("GET", f"/posts/{pid}/comments", {"sort": "top"}),
                ("GET", f"/posts/{pid}/comments", None),
                ("GET", "/comments", {"post_id": pid, "sort": "top"}),
                ("GET", "/comments", {"post_id": pid}),
                ("GET", f"/posts/{pid}/replies", {"sort": "top"}),
                ("GET", f"/posts/{pid}/replies", None),
                ("GET", f"/posts/{pid}/thread", None),
                ("GET", f"/posts/{pid}/discussion", None),
                ("GET", f"/posts/{pid}/comment", None),
            ]
            out = []
            for method, path, params in candidates:
                resp = self.client._request(method, path, params=params)
                body = resp.text or ""
                short = (body[:500] + "…") if len(body) > 500 else body
                out.append(f"PROBE {method} {path} params={params} -> HTTP {resp.status_code}\n{short}\n")
            return "\n".join(out)

        def done(text):
            QMessageBox.information(self, "Probe Results", text)
            self.bus.log_line.emit(text)

        self.run_bg("Probing comments API…", task, done)

    # ---------- Submolts ----------
    @safe_slot
    def on_list_submolts(self):
        def task():
            self.require_key()
            resp = self.client.list_submolts()
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))

            submolts = None
            if isinstance(data, dict):
                for k in ["submolts", "results", "data"]:
                    v = data.get(k)
                    if isinstance(v, list):
                        submolts = v
                        break
                if submolts is None and isinstance(data.get("data"), dict):
                    dd = data["data"]
                    if isinstance(dd.get("submolts"), list):
                        submolts = dd["submolts"]
            if submolts is None:
                raise ValueError("Could not parse submolts list:\n" + json.dumps(data, indent=2, ensure_ascii=False))
            return submolts

        def done(submolts):
            self.submolts_list.clear()
            self.submolt_pick.clear()

            for sm in submolts:
                name = sm.get("name") if isinstance(sm, dict) else None
                disp = sm.get("display_name") if isinstance(sm, dict) else None
                desc = sm.get("description") if isinstance(sm, dict) else None
                if name:
                    self.submolt_pick.addItem(name)
                    line = f"m/{name}"
                    if disp:
                        line += f" — {disp}"
                    if desc:
                        line += f" — {desc[:80]}"
                    self.submolts_list.addItem(line)

            QMessageBox.information(self, "Submolts", f"Loaded {len(submolts)} submolts.")

        self.run_bg("Loading submolts…", task, done)

    @safe_slot
    def on_select_submolt_from_list(self):
        items = self.submolts_list.selectedItems()
        if not items:
            return
        text = items[0].text()
        name = normalize_submolt(text.split()[0])
        if name:
            self.submolt_pick.setCurrentText(name)

    @safe_slot
    def on_load_submolt_info(self):
        name = normalize_submolt(self.submolt_pick.currentText())
        if not name:
            QMessageBox.information(self, "Info", "Pick or type a submolt name.")
            return

        def task():
            self.require_key()
            resp = self.client.get_submolt(name)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Submolt Info", json.dumps(data, indent=2, ensure_ascii=False))

        self.run_bg("Loading submolt…", task, done)

    @safe_slot
    def on_create_submolt(self):
        name = normalize_submolt(self.create_sub_name.text())
        display_name = self.create_sub_display.text().strip()
        desc = self.create_sub_desc.text().strip()
        if not name or not display_name or not desc:
            raise ValueError("Need name, display_name, and description.")

        def task():
            self.require_key()
            resp = self.client.create_submolt(name=name, display_name=display_name, description=desc)
            data = parse_json(self.client, resp)
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Created", json.dumps(data, indent=2, ensure_ascii=False))
            self.on_list_submolts()

        self.run_bg("Creating submolt…", task, done)

    @safe_slot
    def on_subscribe_submolt(self):
        name = normalize_submolt(self.submolt_pick.currentText())
        if not name:
            raise ValueError("Pick a submolt first.")

        def task():
            self.require_key()
            resp = self.client.subscribe_submolt(name)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Subscribed", json.dumps(data, indent=2, ensure_ascii=False))

        self.run_bg("Subscribing…", task, done)

    @safe_slot
    def on_unsubscribe_submolt(self):
        name = normalize_submolt(self.submolt_pick.currentText())
        if not name:
            raise ValueError("Pick a submolt first.")

        def task():
            self.require_key()
            resp = self.client.unsubscribe_submolt(name)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Unsubscribed", json.dumps(data, indent=2, ensure_ascii=False))

        self.run_bg("Unsubscribing…", task, done)

    # ---------- Agents ----------
    @safe_slot
    def on_agent_profile(self):
        name = self.agent_lookup_name.text().strip()
        if not name:
            raise ValueError("Enter a molty name.")

        def task():
            self.require_key()
            resp = self.client.agent_profile(name)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            self.agents_output.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))

        self.run_bg("Loading profile…", task, done)

    @safe_slot
    def on_agent_follow(self):
        name = self.agent_lookup_name.text().strip()
        if not name:
            raise ValueError("Enter a molty name.")

        def task():
            self.require_key()
            resp = self.client.follow_agent(name)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Follow", json.dumps(data, indent=2, ensure_ascii=False))
            self.on_agent_profile()

        self.run_bg("Following…", task, done)

    @safe_slot
    def on_agent_unfollow(self):
        name = self.agent_lookup_name.text().strip()
        if not name:
            raise ValueError("Enter a molty name.")

        def task():
            self.require_key()
            resp = self.client.unfollow_agent(name)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Unfollow", json.dumps(data, indent=2, ensure_ascii=False))
            self.on_agent_profile()

        self.run_bg("Unfollowing…", task, done)

    @safe_slot
    def on_update_me(self):
        desc = self.my_desc.text().strip()
        meta_txt = self.my_metadata.text().strip()
        metadata = None
        if meta_txt:
            metadata = json.loads(meta_txt)

        def task():
            self.require_key()
            resp = self.client.update_me(description=desc if desc else None, metadata=metadata)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Updated", json.dumps(data, indent=2, ensure_ascii=False))

        self.run_bg("Updating profile…", task, done)

    @safe_slot
    def on_upload_my_avatar(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select avatar image", "", "Images (*.png *.jpg *.jpeg *.gif *.webp)")
        if not path:
            return

        def task():
            self.require_key()
            resp = self.client.upload_my_avatar(path)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Avatar Uploaded", json.dumps(data, indent=2, ensure_ascii=False))

        self.run_bg("Uploading avatar…", task, done)

    @safe_slot
    def on_remove_my_avatar(self):
        def task():
            self.require_key()
            resp = self.client.remove_my_avatar()
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            QMessageBox.information(self, "Avatar Removed", json.dumps(data, indent=2, ensure_ascii=False))

        self.run_bg("Removing avatar…", task, done)

    # ---------- Moderation ----------
    @safe_slot
    def on_update_submolt_settings(self):
        name = normalize_submolt(self.mod_submolt.text())
        if not name:
            raise ValueError("Enter a submolt name.")
        desc = self.mod_desc.text().strip() or None
        banner = self.mod_banner_color.text().strip() or None
        theme = self.mod_theme_color.text().strip() or None

        def task():
            self.require_key()
            resp = self.client.update_submolt_settings(name, description=desc, banner_color=banner, theme_color=theme)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            self.mod_output.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))
            QMessageBox.information(self, "Updated", "Submolt settings updated.")

        self.run_bg("Updating settings…", task, done)

    @safe_slot
    def on_upload_submolt_media(self, media_type: str):
        name = normalize_submolt(self.mod_submolt.text())
        if not name:
            raise ValueError("Enter a submolt name.")
        path, _ = QFileDialog.getOpenFileName(self, f"Select {media_type} image", "", "Images (*.png *.jpg *.jpeg *.gif *.webp)")
        if not path:
            return

        def task():
            self.require_key()
            resp = self.client.upload_submolt_media(name, path, media_type=media_type)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            self.mod_output.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))
            QMessageBox.information(self, "Uploaded", f"{media_type} uploaded.")

        self.run_bg(f"Uploading {media_type}…", task, done)

    @safe_slot
    def on_add_moderator(self):
        sub = normalize_submolt(self.mod_submolt.text())
        agent = self.mod_agent.text().strip()
        if not sub or not agent:
            raise ValueError("Need submolt and agent_name.")

        def task():
            self.require_key()
            resp = self.client.add_moderator(sub, agent_name=agent, role="moderator")
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            self.mod_output.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))
            QMessageBox.information(self, "Moderator added", "Done.")

        self.run_bg("Adding moderator…", task, done)

    @safe_slot
    def on_remove_moderator(self):
        sub = normalize_submolt(self.mod_submolt.text())
        agent = self.mod_agent.text().strip()
        if not sub or not agent:
            raise ValueError("Need submolt and agent_name.")

        def task():
            self.require_key()
            resp = self.client.remove_moderator(sub, agent_name=agent)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            self.mod_output.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))
            QMessageBox.information(self, "Moderator removed", "Done.")

        self.run_bg("Removing moderator…", task, done)

    @safe_slot
    def on_list_moderators(self):
        sub = normalize_submolt(self.mod_submolt.text())
        if not sub:
            raise ValueError("Enter submolt.")

        def task():
            self.require_key()
            resp = self.client.list_moderators(sub)
            data = parse_json(self.client, resp)
            if resp.status_code != 200:
                raise ValueError(data.get("error") or json.dumps(data, indent=2, ensure_ascii=False))
            return data

        def done(data):
            self.mod_output.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))

        self.run_bg("Listing moderators…", task, done)


def main():
    import sys
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
