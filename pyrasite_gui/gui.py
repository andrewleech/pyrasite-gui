#!/usr/bin/env python
# This file is part of pyrasite.
#
# pyrasite is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyrasite is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyrasite.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright (C) 2012 Red Hat, Inc., Luke Macken <lmacken@redhat.com>
#
# This interface may contain some code from the gtk-demo, written
# by John (J5) Palmieri, and licensed under the LGPLv2.1
# http://git.gnome.org/browse/pygobject/tree/demos/gtk-demo/gtk-demo.py

from __future__ import division

import os
import sys
import site
import time
import json
import socket
import psutil
import logging
import keyword
import platform
import tempfile
import tokenize
import threading
import subprocess
from functools import partial
from os.path import join, abspath, dirname
from random import randrange
try:
    import meliae
    from meliae import loader
except:
    meliae, loader = None, None
    print("Unable to import meliae. Object memory analysis disabled.")
try:
    from gi.repository import GLib, GObject, Pango, Gtk, WebKit
except ImportError:
    print("Unable to find pygobject3. Please install the 'pygobject3' ")
    print("package on Fedora, or 'python-gobject-dev' on Ubuntu.")
    sys.exit(1)

import pyrasite

log = logging.getLogger('pyrasite')

POLL_INTERVAL = 1.0
INTERVALS = 200
cpu_intervals = []
cpu_details = ''
mem_intervals = []
mem_details = ''
write_intervals = []
read_intervals = []
read_count = read_bytes = write_count = write_bytes = 0

process_title = ''
process_status = ''
thread_intervals = {}
thread_colors = {}
thread_totals = {}

open_connections = []
open_files = []


class Process(pyrasite.PyrasiteIPC, GObject.GObject):
    """
    A :class:`GObject.GObject` subclass that represents a Process, for use in
    the :class:`ProcessTreeStore`
    """


class ProcessListStore(Gtk.ListStore):
    """This TreeStore finds all running python processes."""

    def __init__(self, *args):
        Gtk.ListStore.__init__(self, str, Process, Pango.Style)
        for process in psutil.process_iter():
            pid = process.pid
            if pid != os.getpid():  # ignore self
                try:
                    if 'python' in process.name().lower() or self._check_for_python_lib(process, pid):
                        proc = Process(pid)
                        self.append(("%s: %s" % (pid, proc.title.strip()), proc, Pango.Style.NORMAL))

                except psutil.AccessDenied:
                    pass

    def _check_for_python_lib(self, process, pid):
        if platform.system() == 'Windows':
            # psutils.open_files often doesn't show loaded system libraries on windows
            try:
                import win32api, win32con, win32process
                handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
                for fhandle in win32process.EnumProcessModules(handle):
                    if 'python' in win32process.GetModuleFileNameEx(handle, fhandle).lower():
                        return True
            except:  # Can't inspect process, ignore
                pass
        else:
            open_files = process.open_files()
            if any(('python' in lib.path.lower() for lib in open_files)):
                return True

        return False


class PyrasiteWindow(Gtk.Window):

    def __init__(self):
        super(PyrasiteWindow, self).__init__(type=Gtk.WindowType.TOPLEVEL)

        self.processes = {}
        self.pid = None  # Currently selected pid
        self.resource_thread = None

        self.set_title('Pyrasite v%s' % pyrasite.__version__)
        self.set_default_size(1024, 600)

        hbox = Gtk.HBox(homogeneous=False, spacing=0)
        self.add(hbox)

        tree = self.create_tree()
        hbox.pack_start(tree, False, False, 0)

        notebook = Gtk.Notebook()

        main_vbox = Gtk.VBox()
        main_vbox.pack_start(notebook, True, True, 0)
        self.progress = Gtk.ProgressBar()
        main_vbox.pack_end(self.progress, False, False, 0)
        hbox.pack_start(main_vbox, True, True, 0)

        self.info_html = ''
        self.info_view = WebKit.WebView()
        self.info_view.load_string(self.info_html, "text/html", "utf-8", '#')

        info_window = Gtk.ScrolledWindow(hadjustment=None, vadjustment=None)
        info_window.set_policy(Gtk.PolicyType.AUTOMATIC,
                               Gtk.PolicyType.AUTOMATIC)
        info_window.add(self.info_view)
        notebook.append_page(info_window,
                Gtk.Label.new_with_mnemonic('_Resources'))

        (stacks_widget, source_buffer) = self.create_text(True)
        notebook.append_page(stacks_widget,
                Gtk.Label.new_with_mnemonic('_Stacks'))

        self.source_buffer = source_buffer
        self.source_buffer.create_tag('bold', weight=Pango.Weight.BOLD)
        self.source_buffer.create_tag('italic', style=Pango.Style.ITALIC)
        self.source_buffer.create_tag('comment', foreground='#c0c0c0')
        self.source_buffer.create_tag('decorator', foreground='#7d7d7d',
                                      style=Pango.Style.ITALIC)
        self.source_buffer.create_tag('keyword', foreground='#0000ff')
        self.source_buffer.create_tag('number', foreground='#800000')
        self.source_buffer.create_tag('string', foreground='#00aa00',
                                      style=Pango.Style.ITALIC)

        self.obj_tree = obj_tree = Gtk.TreeView()
        self.obj_store = obj_store = Gtk.ListStore(str, int, int, int,
                                                   int, int, int, str)
        obj_tree.set_model(obj_store)
        obj_selection = obj_tree.get_selection()
        obj_selection.set_mode(Gtk.SelectionMode.BROWSE)
        obj_tree.set_size_request(200, -1)

        columns = [
            Gtk.TreeViewColumn(title='Count',
                               cell_renderer=Gtk.CellRendererText(),
                               text=1, style=2),
            Gtk.TreeViewColumn(title='%',
                               cell_renderer=Gtk.CellRendererText(),
                               text=2, style=2),
            Gtk.TreeViewColumn(title='Size',
                               cell_renderer=Gtk.CellRendererText(),
                               text=3, style=2),
            Gtk.TreeViewColumn(title='%',
                               cell_renderer=Gtk.CellRendererText(),
                               text=4, style=2),
            Gtk.TreeViewColumn(title='Cumulative',
                               cell_renderer=Gtk.CellRendererText(),
                               text=5, style=2),
            Gtk.TreeViewColumn(title='Max',
                               cell_renderer=Gtk.CellRendererText(),
                               text=6, style=2),
            Gtk.TreeViewColumn(title='Kind',
                               cell_renderer=Gtk.CellRendererText(),
                               text=7, style=2),
            ]

        first_iter = obj_store.get_iter_first()
        if first_iter is not None:
            obj_selection.select_iter(first_iter)

        obj_selection.connect('changed', self.obj_selection_cb, obj_store)
        obj_tree.connect('row_activated', self.obj_row_activated_cb, obj_store)

        for i, column in enumerate(columns):
            column.set_sort_column_id(i + 1)
            obj_tree.append_column(column)

        obj_tree.collapse_all()
        obj_tree.set_headers_visible(True)

        scrolled_window = Gtk.ScrolledWindow(hadjustment=None,
                                             vadjustment=None)
        scrolled_window.set_policy(Gtk.PolicyType.NEVER,
                                   Gtk.PolicyType.AUTOMATIC)
        scrolled_window.add(obj_tree)

        hbox = Gtk.VBox(homogeneous=False, spacing=0)

        bar = Gtk.InfoBar()
        hbox.pack_start(bar, False, False, 0)
        bar.set_message_type(Gtk.MessageType.INFO)
        self.obj_totals = Gtk.Label()
        bar.get_content_area().pack_start(self.obj_totals, False, False, 0)
        hbox.pack_start(bar, False, False, 0)

        hbox.pack_start(scrolled_window, True, True, 0)
        (text_widget, obj_buffer) = self.create_text(False)
        self.obj_buffer = obj_buffer
        hbox.pack_end(text_widget, True, True, 0)

        notebook.append_page(hbox, Gtk.Label.new_with_mnemonic('_Objects'))

        (shell_view, shell_widget, shell_buffer) = \
                self.create_text(False, return_view=True)
        self.shell_view = shell_view
        self.shell_buffer = shell_buffer
        self.shell_widget = shell_widget
        shell_hbox = Gtk.VBox()
        shell_hbox.pack_start(shell_widget, True, True, 0)
        shell_bottom = Gtk.HBox()

        shell_prompt = Gtk.Entry()
        self.shell_prompt = shell_prompt
        self.shell_prompt.connect('activate', self.run_shell_command)
        shell_bottom.pack_start(shell_prompt, True, True, 0)

        self.shell_button = shell_button = Gtk.Button('Run')
        shell_button.connect('clicked', self.run_shell_command)
        shell_bottom.pack_start(shell_button, False, False, 0)
        shell_hbox.pack_end(shell_bottom, False, False, 0)

        shell_label = Gtk.Label.new_with_mnemonic('_Shell')
        notebook.append_page(shell_hbox, shell_label)

        # To try and grab focus of our text input
        notebook.connect('switch-page', self.switch_page)
        self.notebook = notebook

        graph_vbox = Gtk.VBox()
        graph_spinner_box = Gtk.HBox(False, 0)
        graph_vbox.pack_start(graph_spinner_box, False, False, 0)

        label = Gtk.Label("Sample size(seconds): ")
        label.set_alignment(0, 0.5)
        graph_spinner_box.pack_start(label, False, False, 0)

        adj = Gtk.Adjustment(1.0, 1.0, 60.0, 1.0, 5.0, 0.0)
        self.spinner = spinner = Gtk.SpinButton()
        spinner.configure(adj, 0, 0)
        spinner.set_wrap(False)
        graph_spinner_box.pack_start(spinner, False, False, 0)

        self.spinner_button = spinner_button = Gtk.Button('Go')
        spinner_button.connect('clicked', self.sample_call_tree)
        graph_spinner_box.pack_start(spinner_button, False, False, 0)

        scrolled_window = Gtk.ScrolledWindow(hadjustment=None,
                                             vadjustment=None)
        scrolled_window.set_policy(Gtk.PolicyType.ALWAYS,
                                   Gtk.PolicyType.ALWAYS)

        self.call_graph = Gtk.Image()
        scrolled_window.add_with_viewport(self.call_graph)

        graph_vbox.pack_start(scrolled_window, True, True, 0)
        notebook.append_page(graph_vbox,
                Gtk.Label.new_with_mnemonic('_Call Graph'))

        self.details_html = ''
        self.details_view = WebKit.WebView()
        self.details_view.load_string(self.details_html, "text/html",
                                      "utf-8", '#')

        details_window = Gtk.ScrolledWindow(hadjustment=None,
                                            vadjustment=None)
        details_window.set_policy(Gtk.PolicyType.AUTOMATIC,
                               Gtk.PolicyType.AUTOMATIC)
        details_window.add(self.details_view)
        notebook.append_page(details_window,
                Gtk.Label.new_with_mnemonic('_Details'))

        self.show_all()
        self.progress.hide()

        # Load up our javascript resources
        js = join(dirname(abspath(__file__)), 'js')
        if not os.path.isdir(js):
            js = '/usr/lib/javascript/'
        jquery_js = open(join(js, 'jquery-1.7.1.min.js'))
        self.jquery_js = jquery_js.read()
        jquery_js.close()
        jquery_sparkline_js = open(join(js, 'jquery.sparkline.min.js'))
        self.jquery_sparkline_js = jquery_sparkline_js.read()
        jquery_sparkline_js.close()

    def sample_call_tree(self, widget):
        self.generate_callgraph(sample_size=self.spinner.get_value())

    def switch_page(self, notebook, page, pagenum):
        name = self.notebook.get_tab_label(self.notebook.get_nth_page(pagenum))
        if name.get_text() == 'Shell':
            GObject.timeout_add(0, self.shell_prompt.grab_focus)

    def run_shell_command(self, widget):
        cmd = self.shell_prompt.get_text()
        end = self.shell_buffer.get_end_iter()
        self.shell_buffer.insert(end, '\n>>> %s\n' % cmd)
        log.debug("run_shell_command(%r)" % cmd)
        output = self.proc.cmd(cmd)
        log.debug(repr(output))
        self.shell_buffer.insert(end, output)
        self.shell_prompt.set_text('')

        insert_mark = self.shell_buffer.get_insert()
        self.shell_buffer.place_cursor(self.shell_buffer.get_end_iter())
        self.shell_view.scroll_to_mark(insert_mark, 0.0, True, 0.0, 1.0)

    def obj_selection_cb(self, selection, model):
        sel = selection.get_selected()
        treeiter = sel[1]
        address = model.get_value(treeiter, 0)
        value = pyrasite.inspect(self.pid, address)
        if value:
            self.obj_buffer.set_text(value)
        else:
            self.obj_buffer.set_text('Unable to inspect object. Make sure you '
                    'have the python debugging symbols installed.')

    def obj_row_activated_cb(self, *args, **kw):
        log.debug("obj_row_activated_cb(%s, %s)" % (args, kw))

    def generate_description(self, title):
        p = psutil.Process(self.proc.pid)

        self.info_html = """
        <html><head>
            <style>
            body {font: normal 12px/150%% Arial, Helvetica, sans-serif;}
            .grid table {
                border-collapse: collapse;
                text-align: left;
                width: 100%%;
            }
            .grid {
                font: normal 12px/150%% Arial, Helvetica, sans-serif;
                background: #fff; overflow: hidden; border: 1px solid #2e3436;
                -webkit-border-radius: 3px; border-radius: 3px;
            }
            .grid table td, .grid table th { padding: 3px 10px; }
            .grid table thead th {
                background:-webkit-gradient(linear, left top, left bottom,
                                            color-stop(0.05, #888a85),
                                            color-stop(1, #555753) );
                background-color:#2e3436; color:#FFFFFF; font-size: 15px;
                font-weight: bold; border-left: 1px solid #2e3436; }
            .grid table thead th:first-child { border: none; }
            .grid table tbody td {
                color: #2e3436;
                border-left: 1px solid #2e3436;
                font-size: 12px;
                font-weight: normal;
            }
            .grid table tbody .alt td { background: #d3d7cf; color: #2e3436; }
            .grid table tbody td:first-child { border: none; }
            </style>
        </head>
        <body>
            <h2 id="proc_title">%(title)s</h2>
                <div class="grid">
                <table>
                    <thead><tr>
                        <th width="50%%">CPU: <span id="cpu_details"/></th>
                        <th width="50%%">Memory: <span id="mem_details"/></th>
                    </tr></thead>
                    <tbody>
                        <tr>
                            <td>
                                <span id="cpu_graph" class="cpu_graph"></span>
                            </td>
                            <td>
                                <span id="mem_graph" class="mem_graph"></span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
            <br/>
            <div class="grid">
                <table>
                    <thead><tr>
                        <th width="50%%">Read: <span id="read_details"/></th>
                        <th width="50%%">Write: <span id="write_details"/></th>
                    </tr></thead>
                    <tbody>
                        <tr><td><span id="read_graph"></span></td>
                            <td><span id="write_graph"></span></td></tr>
                    </tbody>
                </table>
            </div>
            <br/>
            <div class="grid">
                <table>
                    <thead>
                        <tr><th>Threads</th></tr>
                    </thead>
                    <tbody>
                        <tr><td><span id="thread_graph"></span></td></tr>
                    </tbody>
                </table>
            </div>
            <br/>
        """ % dict(title=self.proc.title)

        global process_title, process_status
        process_title = self.proc.title
        process_status = ""

        self.info_html += """
        <div class="grid">
            <table>
                <thead><tr><th>Open Files</th></tr></thead>
                <tbody id="open_files"></tbody>
            </table>
        </div>
        <br/>
        <div class="grid">
            <table>
                <thead><tr><th colspan="4">Connections</th></tr></thead>
                <tbody id="open_connections"></tbody>
            </table>
        </div>
        </body></html>
        """

        self.info_view.load_string(self.info_html, "text/html", "utf-8", '#')

        # The Details tab
        try:
            uid = p.uids().real
            gid = p.gids().real
        except AttributeError:
            uid = "n/a"
            gid = "n/a"

        self.details_html = """
        <style>
        body {font: normal 12px/150%% Arial, Helvetica, sans-serif;}
        </style>
        <h2>%s</h2>
        <ul>
            <li><b>status:</b> %s</li>
            <li><b>cwd:</b> %s</li>
            <li><b>cmdline:</b> %s</li>
            <li><b>terminal:</b> %s</li>
            <li><b>created:</b> %s</li>
            <li><b>username:</b> %s</li>
            <li><b>uid:</b> %s</li>
            <li><b>gid:</b> %s</li>
            <li><b>nice:</b> %s</li>
        </ul>
        """ % (self.proc.title, p.status, p.cwd(), ' '.join(p.cmdline()),
               getattr(p, 'terminal', 'unknown'), time.ctime(p.create_time()),
               p.username(), uid, gid, p.nice())

        self.details_view.load_string(self.details_html, "text/html",
                                      "utf-8", '#')

        if not self.resource_thread:
            self.resource_thread = ResourceUsagePoller(self.proc.pid)
            self.resource_thread.daemon = True
            self.resource_thread.info_view = self.info_view
            self.resource_thread.start()
        self.resource_thread.process = p

        GObject.timeout_add(100, self.inject_js)
        GObject.timeout_add(int(POLL_INTERVAL * 1000),
                            self.render_resource_usage)

    def inject_js(self):
        log.debug("Injecting jQuery")
        self.info_view.execute_script(self.jquery_js)
        self.info_view.execute_script(self.jquery_sparkline_js)

    def render_resource_usage(self):
        """
        Render our resource usage using jQuery+Sparklines in our WebKit view
        """
        global cpu_intervals, mem_intervals, cpu_details, mem_details
        global read_intervals, write_intervals, read_bytes, write_bytes
        global open_files, open_connections
        global process_title, process_status
        script = """
            jQuery('#cpu_graph').sparkline(%s, {'height': 75, 'width': 250,
                spotRadius: 3, fillColor: '#73d216', lineColor: '#4e9a06'});
            jQuery('#mem_graph').sparkline(%s, {'height': 75, 'width': 250,
                lineColor: '#5c3566', fillColor: '#75507b',
                minSpotColor: false, maxSpotColor: false, spotColor: '#f57900',
                spotRadius: 3});
            jQuery('#cpu_details').text('%s');
            jQuery('#mem_details').text('%s');
            jQuery('#read_graph').sparkline(%s, {'height': 75, 'width': 250,
                lineColor: '#a40000', fillColor: '#cc0000',
                minSpotColor: false, maxSpotColor: false, spotColor: '#729fcf',
                spotRadius: 3});
            jQuery('#write_graph').sparkline(%s, {'height': 75, 'width': 250,
                lineColor: '#ce5c00', fillColor: '#f57900',
                minSpotColor: false, maxSpotColor: false, spotColor: '#8ae234',
                spotRadius: 3});
            jQuery('#read_details').text('%s');
            jQuery('#write_details').text('%s');
        """ % (cpu_intervals, mem_intervals, cpu_details, mem_details,
               read_intervals, write_intervals, humanize_bytes(read_bytes),
               humanize_bytes(write_bytes))

        for i, thread in enumerate(thread_intervals):
            script += """
                jQuery('#thread_graph').sparkline(%s, {
                    %s'lineColor': '#%s', 'fillColor': false, 'spotRadius': 3,
                    'spotColor': '#%s'});
            """ % (thread_intervals[thread], i != 0 and "'composite': true,"
                   or "'height': 75, 'width': 575,", thread_colors[thread],
                   thread_colors[thread])

        if open_files:
            script += """
                jQuery('#open_files').html('%s');
            """ % ''.join(['<tr%s><td>%s</td></tr>' %
                           (i % 2 and ' class="alt"' or '', open_file)
                           for i, open_file in enumerate(open_files)])

        if open_connections:
            row = '<tr%s><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
            script += """
                jQuery('#open_connections').html('%s');
            """ % ''.join([row % (i % 2 and ' class="alt"' or '',
                                  conn['type'], conn['local'],
                                  conn['remote'], conn['status'])
                           for i, conn in enumerate(open_connections)])

        script += """
            jQuery('#proc_title').text('%s %s');
        """ % (str(process_title).strip(), process_status)
        self.info_view.execute_script(script)
        return True

    def _section_progress(self, start, end, fraction, text=None):
        self.update_progress(start + ((end - start) * fraction), text)

    def section_progress(self, start, end):
        return partial(self._section_progress, start, end)

    def update_progress(self, fraction, text=None):
        if text:
            self.progress.set_text(text + '...')
            self.progress.set_show_text(True)
        if fraction:
            self.progress.set_fraction(fraction)
        else:
            self.progress.pulse()
        while Gtk.events_pending():
            Gtk.main_iteration()

    def selection_cb(self, selection, model):
        sel = selection.get_selected()
        if sel == ():
            return

        self.progress.show()
        self.update_progress(0.1, "Analyzing process")

        treeiter = sel[1]
        title = model.get_value(treeiter, 0)
        proc = model.get_value(treeiter, 1)  # type: Process
        self.proc = proc

        if self.pid and proc.pid != self.pid:
            global cpu_intervals, mem_intervals, write_intervals, \
                   read_intervals, cpu_details, mem_details, read_count, \
                   read_bytes, thread_totals, write_count, write_bytes, \
                   thread_intervals, thread_colors, open_files, \
                   open_connections
            cpu_intervals = [0.0]
            mem_intervals = []
            write_intervals = []
            read_intervals = []
            cpu_details = mem_details = ''
            read_count = read_bytes = write_count = write_bytes = 0
            thread_intervals = {}
            thread_colors = {}
            thread_totals = {}
            open_connections = []
            open_files = []

        self.pid = proc.pid

        # Analyze the process
        self.generate_description(title)

        # Inject a reverse subshell
        self.update_progress(0.2, "Injecting reverse connection")
        if proc.title not in self.processes:
            proc.connect()
            self.processes[proc.title] = proc

        # Add local env path and site-packages to target python path
        self.update_progress(0.25, "Injecting python paths")
        self.add_paths()

        # Dump stacks
        self.dump_stacks(self.section_progress(0.3, 0.4))

        ## Call Stack
        self.generate_callgraph(1, self.section_progress(0.45, 0.6))

        # Dump objects and load them into our store
        try:
            self.dump_objects(self.section_progress(0.65, 0.85))
        except socket.timeout:
            log.info('dump_objects() timed out')

        # Shell
        self.update_progress(0.9, "Determining Python version")
        self.shell_buffer.set_text(
                proc.cmd('import sys; print("Python " + sys.version)'))

        self.fontify()
        self.update_progress(1.0)
        self.progress.hide()
        self.update_progress(0.0)

    def add_paths(self):
        env_paths = []
        for app in ['dot', 'gdb']:
            app_path = which(app)
            if app_path:
                app_dir = os.path.dirname(app_path)
                env_paths.append(os.path.abspath(app_dir))

        env_paths.append('os.environ["PATH"]')
        # env_paths_str = ','.join(['r"%s"' % os.path.abspath(p) for p in env_paths])
        # py_paths_str = ','.join(['r"%s"' % os.path.abspath(p) for p in site.getsitepackages()])
        env_paths_str = json.dumps(env_paths)
        py_paths_str = json.dumps(site.getsitepackages())

        cmd = ';'.join([
            'import os, sys',
            'os.environ["PATH"] = os.pathsep.join(%s)' % env_paths_str,
            'sys.path.extend(%s)' % py_paths_str
        ])

        output = self.proc.cmd(cmd)
        if output:
            log.debug(output)

    def dump_objects(self, update_progress):
        update_progress(0, "Dumping all objects")
        cmd = '\n'.join(["import os, shutil, tempfile, threading",
                         "from meliae import scanner",
                         "tmp = os.path.join(tempfile.gettempdir(), str(os.getpid()))",
                         "def background_dump():",
                         "    scanner.dump_all_objects(tmp + '.json')",
                         "    shutil.move(tmp + '.json', tmp + '.objects')",
                         "threading.Thread(target=background_dump).start()"])
        output = self.proc.cmd(cmd)
        if 'No module named meliae' in output:
            log.error('Error: %s is unable to import `meliae`' %
                      self.proc.title.strip())
            return
        update_progress(0.25)

        # Clear previous model
        self.obj_store.clear()
        update_progress(0.5, "Loading object dump")

        tmp = os.path.join(tempfile.gettempdir(), str(self.proc.pid))
        temp_file = tmp + '.json'
        objects_file = tmp + '.objects'
        now = time.time()
        if os.path.exists(temp_file):
            while time.time() - now < 10*60:  # 10 minute timeout
                if not os.path.exists(objects_file):
                    time.sleep(3)
                else:
                    try:
                        objects = loader.load(objects_file, show_prog=False)
                    except NameError:
                        log.debug("Meliae not available, continuing...")
                        return
                    except:
                        log.debug("Falling back to slower meliae object dump loader")
                        objects = loader.load(objects_file, show_prog=False, using_json=False)

                    objects.compute_referrers()
                    update_progress(0.75)
                    summary = objects.summarize()
                    update_progress(0.9)

                    def intify(x):
                        try:
                            return int(x)
                        except:
                            return x

                    for i, line in enumerate(str(summary).split('\n')):
                        if i == 0:
                            self.obj_totals.set_text(line)
                        elif i == 1:
                            continue  # column headers
                        else:
                            obj = summary.summaries[i - 2]
                            self.obj_store.append([str(obj.max_address)] +
                                                   map(intify, line.split()[1:]))
                    os.unlink(objects_file)
                    break
            update_progress(1)

    def dump_stacks(self, update_progress):
        update_progress(0, "Dumping stacks")
        payloads = os.path.join(os.path.abspath(os.path.dirname(
            pyrasite.__file__)), 'payloads')
        dump_stacks = os.path.join(payloads, 'dump_stacks.py')
        code = self.proc.cmd(open(dump_stacks).read())
        update_progress(1)

        self.source_buffer.set_text('')
        start = self.source_buffer.get_iter_at_offset(0)
        end = start.copy()
        self.source_buffer.insert(end, code)

    def generate_callgraph(self, sample_size=1, update_progress=None):
        if update_progress:
            update_progress(0, "Tracing call stack for %d seconds" % sample_size)

        graphviz_path = which('dot')

        image = os.path.join(tempfile.gettempdir(), "%d-callgraph.png" % self.proc.pid)

        out = self.proc.cmd(';'.join(('import pycallgraph',
                                      'from pycallgraph.output import GraphvizOutput',
                                      '_output = GraphvizOutput()',
                                      '_output.tool=r"%s"' % graphviz_path,
                                      '_output.output_file=r"%s"' % image,
                                      'pycallgraph._pycallgraph = pycallgraph.PyCallGraph(output=_output)',
                                      'pycallgraph._pycallgraph.start()')))
        if out:
            log.warn(out)

        if update_progress:
            update_progress(0.5)

        time.sleep(sample_size)

        if update_progress:
            update_progress(1.0, "Generating call stack graph")

        self.proc.cmd('import pycallgraph; pycallgraph._pycallgraph.done()')
        self.call_graph.set_from_file(image)

    def row_activated_cb(self, view, path, col, store):
        iter = store.get_iter(path)
        proc = store.get_value(iter, 1)
        if proc is not None:
            store.set_value(iter, 2, Pango.Style.NORMAL)

    def create_tree(self):
        tree_store = ProcessListStore()
        tree_view = Gtk.TreeView()
        self.tree_view = tree_view
        tree_view.set_model(tree_store)
        selection = tree_view.get_selection()
        selection.set_mode(Gtk.SelectionMode.BROWSE)
        tree_view.set_size_request(200, -1)

        cell = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn(title='Processes', cell_renderer=cell,
                                    text=0, style=2)

        first_iter = tree_store.get_iter_first()
        if first_iter is not None:
            selection.select_iter(first_iter)

        selection.connect('changed', self.selection_cb, tree_store)
        tree_view.connect('row_activated', self.row_activated_cb, tree_store)

        tree_view.append_column(column)

        tree_view.collapse_all()
        tree_view.set_headers_visible(False)
        scrolled_window = Gtk.ScrolledWindow(hadjustment=None,
                                             vadjustment=None)
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC,
                                   Gtk.PolicyType.AUTOMATIC)

        scrolled_window.add(tree_view)

        label = Gtk.Label(label='Processes')

        box = Gtk.Notebook()
        box.set_size_request(250, -1)
        box.append_page(scrolled_window, label)

        tree_view.grab_focus()

        return box

    def create_text(self, is_source, return_view=False):
        scrolled_window = Gtk.ScrolledWindow(hadjustment=None,
                                             vadjustment=None)
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC,
                                   Gtk.PolicyType.AUTOMATIC)
        scrolled_window.set_shadow_type(Gtk.ShadowType.IN)

        text_view = Gtk.TextView()
        buffer = Gtk.TextBuffer()

        text_view.set_buffer(buffer)
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)

        scrolled_window.add(text_view)

        if is_source:
            font_desc = Pango.FontDescription('monospace')
            text_view.modify_font(font_desc)
            text_view.set_wrap_mode(Gtk.WrapMode.NONE)
        else:
            text_view.set_wrap_mode(Gtk.WrapMode.WORD)
            text_view.set_pixels_above_lines(2)
            text_view.set_pixels_below_lines(2)

        if return_view:
            return (text_view, scrolled_window, buffer)
        return(scrolled_window, buffer)

    def fontify(self):
        start_iter = self.source_buffer.get_iter_at_offset(0)
        end_iter = self.source_buffer.get_iter_at_offset(0)
        data = self.source_buffer.get_text(self.source_buffer.get_start_iter(),
                                           self.source_buffer.get_end_iter(),
                                           False)

        if sys.version_info < (3, 0):
            data = data.decode('utf-8')

        builtin_constants = ['None', 'True', 'False']
        is_decorator = False
        is_func = False

        def prepare_iters():
            start_iter.set_line(srow - 1)
            start_iter.set_line_offset(scol)
            end_iter.set_line(erow - 1)
            end_iter.set_line_offset(ecol)

        try:
            for x in tokenize.generate_tokens(InputStream(data).readline):
                # x has 5-tuples
                tok_type, tok_str = x[0], x[1]
                srow, scol = x[2]
                erow, ecol = x[3]

                if tok_type == tokenize.COMMENT:
                    prepare_iters()
                    self.source_buffer.apply_tag_by_name('comment', start_iter,
                                                         end_iter)
                elif tok_type == tokenize.NAME:
                    if (tok_str in keyword.kwlist or
                        tok_str in builtin_constants):
                        prepare_iters()
                        self.source_buffer.apply_tag_by_name('keyword',
                                                             start_iter,
                                                             end_iter)
                        if tok_str == 'def' or tok_str == 'class':
                            # Next token is going to be a
                            # function/method/class name
                            is_func = True
                            continue
                    elif tok_str == 'self':
                        prepare_iters()
                        self.source_buffer.apply_tag_by_name('italic',
                                                             start_iter,
                                                             end_iter)
                    else:
                        if is_func is True:
                            prepare_iters()
                            self.source_buffer.apply_tag_by_name('bold',
                                                                 start_iter,
                                                                 end_iter)
                        elif is_decorator is True:
                            prepare_iters()
                            self.source_buffer.apply_tag_by_name('decorator',
                                                                 start_iter,
                                                                 end_iter)
                elif tok_type == tokenize.STRING:
                    prepare_iters()
                    self.source_buffer.apply_tag_by_name('string', start_iter,
                                                         end_iter)
                elif tok_type == tokenize.NUMBER:
                    prepare_iters()
                    self.source_buffer.apply_tag_by_name('number', start_iter,
                                                         end_iter)
                elif tok_type == tokenize.OP:
                    if tok_str == '@':
                        prepare_iters()
                        self.source_buffer.apply_tag_by_name('decorator',
                                                             start_iter,
                                                             end_iter)

                        # next token is going to be the decorator name
                        is_decorator = True
                        continue

                if is_func is True:
                    is_func = False

                if is_decorator is True:
                    is_decorator = False
        except tokenize.TokenError:
            pass

    def close(self):
        self.progress.show()
        self.update_progress(None, "Shutting down")
        log.debug("Closing %r" % self)
        for process in self.processes.values():
            self.update_progress(None)
            process.close()
            callgraph = '/tmp/%d-callgraph.png' % process.pid
            if os.path.exists(callgraph):
                os.unlink(callgraph)


##
## Background Threads
##

class ResourceUsagePoller(threading.Thread):
    """A thread for polling a processes CPU & memory usage"""
    process = None

    def __init__(self, pid):
        super(ResourceUsagePoller, self).__init__()
        self.process = psutil.Process(pid)

    def run(self):
        while True:
            try:
                if self.process:
                    self.poll_cpu()
                    self.poll_mem()
                    self.poll_io()
                    self.poll_threads()
                    self.poll_connections()
                    self.poll_files()
                else:
                    time.sleep(1)
            except psutil.NoSuchProcess:
                log.warn("Lost Process")
                self.process = None
                global process_status
                process_status = '[Terminated]'

    def poll_cpu(self):
        global cpu_intervals, cpu_details
        if len(cpu_intervals) >= INTERVALS:
            cpu_intervals = cpu_intervals[1:]
        cpu_intervals.append(float(
            self.process.cpu_percent(interval=POLL_INTERVAL)))
        cputimes = self.process.cpu_times()
        cpu_details = '%0.2f%% (%s user, %s system)' % (
                cpu_intervals[-1], cputimes.user, cputimes.system)

    def poll_mem(self):
        global mem_intervals, mem_details
        if len(mem_intervals) >= INTERVALS:
            mem_intervals = mem_intervals[1:]
        mem_intervals.append(float(self.process.memory_info().rss))
        meminfo = self.process.memory_info()
        mem_details = '%0.2f%% (%s RSS, %s VMS)' % (
                self.process.memory_percent(),
                humanize_bytes(meminfo.rss),
                humanize_bytes(meminfo.vms))

    def poll_io(self):
        global read_count, read_bytes, write_count, write_bytes
        global read_intervals, write_intervals
        if len(read_intervals) >= INTERVALS:
            read_intervals = read_intervals[1:]
            write_intervals = write_intervals[1:]
        io = self.process.io_counters()
        read_since_last = io.read_bytes - read_bytes
        read_intervals.append(float(read_since_last))
        read_count = io.read_count
        read_bytes = io.read_bytes
        write_since_last = io.write_bytes - write_bytes
        write_intervals.append(float(write_since_last))
        write_count = io.write_count
        write_bytes = io.write_bytes

    def poll_threads(self):
        global thread_intervals
        for thread in self.process.threads():
            if thread.id not in thread_intervals:
                thread_intervals[thread.id] = []
                thread_colors[thread.id] = get_color()
                thread_totals[thread.id] = 0.0

            if len(thread_intervals[thread.id]) >= INTERVALS:
                thread_intervals[thread.id] = \
                        thread_intervals[thread.id][1:INTERVALS]

            # FIXME: we should figure out some way to visually
            # distinguish between user and system time.
            total = thread.system_time + thread.user_time
            amount_since = total - thread_totals[thread.id]
            thread_intervals[thread.id].append(
                    float('%.2f' % amount_since))
            thread_totals[thread.id] = total

    def poll_connections(self):
        global open_connections
        connections = []
        for i, conn in enumerate(self.process.connections()):
            if conn.type == socket.SOCK_STREAM:
                type = 'TCP'
            elif conn.type == socket.SOCK_DGRAM:
                type = 'UDP'
            else:
                type = 'UNIX'
            lip, lport = conn.laddr
            if not conn.raddr:
                rip = rport = '*'
            else:
                rip, rport = conn.raddr
            connections.append({
                'type': type,
                'status': conn.status,
                'local': '%s:%s' % (lip, lport),
                'remote': '%s:%s' % (rip, rport),
                })
        open_connections = connections

    def poll_files(self):
        global open_files
        files = []
        for open_file in self.process.open_files():
            files.append(open_file.path.replace('\\', '\\\\'))
        open_files = files


##
## Utilities
##

class InputStream(object):
    '''
    Simple Wrapper for File-like objects. [c]StringIO doesn't provide
    a readline function for use with generate_tokens.
    Using a iterator-like interface doesn't succeed, because the readline
    function isn't used in such a context. (see <python-lib>/tokenize.py)
    '''
    def __init__(self, data):
        self.__data = ['%s\n' % x for x in data.splitlines()]
        self.__lcount = 0

    def readline(self):
        try:
            line = self.__data[self.__lcount]
            self.__lcount += 1
        except IndexError:
            line = ''
            self.__lcount = 0

        return line


def get_color():
    """Prefer tango colors for our lines. Fall back to random ones."""
    tango = ['c4a000', 'ce5c00', '8f5902', '4e9a06', '204a87',
             '5c3566', 'a40000', '555753']
    used = thread_colors.values()
    for color in tango:
        if color not in used:
            return color
    return "".join([hex(randrange(0, 255))[2:] for i in range(3)])


def humanize_bytes(bytes, precision=1):
    """Return a humanized string representation of a number of bytes.
    http://code.activestate.com/recipes/577081-humanized-representation-of-a-number-of-bytes/
    """
    abbrevs = (
        (1 << 50, 'PB'),
        (1 << 40, 'TB'),
        (1 << 30, 'GB'),
        (1 << 20, 'MB'),
        (1 << 10, 'kB'),
        (1, 'bytes')
    )
    if bytes == 1:
        return '1 byte'
    for factor, suffix in abbrevs:
        if bytes >= factor:
            break
    return '%.*f %s' % (precision, bytes / factor, suffix)


def setup_logger(verbose=False):
    """Based on code from Will Maier's 'ideal Python script'.
    https://github.com/wcmaier/python-script
    """
    # NullHandler was added in Python 3.1.
    try:
        NullHandler = logging.NullHandler
    except AttributeError:
        class NullHandler(logging.Handler):
            def emit(self, record):
                pass

    # Add a do-nothing NullHandler to the module logger to prevent "No handlers
    # could be found" errors. The calling code can still add other, more useful
    # handlers, or otherwise configure logging.
    log = logging.getLogger('pyrasite')
    log.addHandler(NullHandler())

    level = logging.INFO
    if verbose:
        level = logging.DEBUG

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    handler.setLevel(level)
    log.addHandler(handler)
    log.setLevel(level)

    return log


def check_depends():
    try:
        # call dot command with null input file.
        # throws exception if command "dot" not found
        subprocess.call(['dot', '-V'], shell=False)
        assert which('dot')
    except OSError:
        print('WARNING: graphviz dot command not found. ' +
              'Call graph will not be available')


def which(cmd, mode=os.F_OK | os.X_OK, path=None):
    """Given a command, mode, and a PATH string, return the path which
    conforms to the given mode on the PATH, or None if there is no such
    file.

    `mode` defaults to os.F_OK | os.X_OK. `path` defaults to the result
    of os.environ.get("PATH"), or can be overridden with a custom search
    path.

    """
    # Check that a given file can be accessed with the correct mode.
    # Additionally check that `file` is not a directory, as on Windows
    # directories pass the os.access check.
    def _access_check(fn, mode):
        return (os.path.exists(fn) and os.access(fn, mode)
                and not os.path.isdir(fn))

    # If we're given a path with a directory part, look it up directly rather
    # than referring to PATH directories. This includes checking relative to the
    # current directory, e.g. ./script
    if os.path.dirname(cmd):
        if _access_check(cmd, mode):
            return cmd
        return None

    if path is None:
        path = os.environ.get("PATH", os.defpath)
    if not path:
        return None
    path = path.split(os.pathsep)

    if sys.platform == "win32":
        # The current directory takes precedence on Windows.
        if not os.curdir in path:
            path.insert(0, os.curdir)

        # PATHEXT is necessary to check on Windows.
        pathext = os.environ.get("PATHEXT", "").split(os.pathsep)
        # See if the given file matches any of the expected path extensions.
        # This will allow us to short circuit when given "python.exe".
        # If it does match, only test that one, otherwise we have to try
        # others.
        if any(cmd.lower().endswith(ext.lower()) for ext in pathext):
            files = [cmd]
        else:
            files = [cmd + ext for ext in pathext]
    else:
        # On other platforms you don't have things like PATHEXT to tell you
        # what file suffixes are executable, so just pass on cmd as-is.
        files = [cmd]

    seen = set()
    for dir in path:
        normdir = os.path.normcase(dir)
        if not normdir in seen:
            seen.add(normdir)
            for thefile in files:
                name = os.path.join(dir, thefile)
                if _access_check(name, mode):
                    return name
    return None


def main():
    check_depends()

    GObject.threads_init()
    mainloop = GLib.MainLoop()

    window = PyrasiteWindow()
    window.show()

    def quit(widget, event, mainloop):
        window.close()
        mainloop.quit()

    window.connect('delete-event', quit, mainloop)

    try:
        mainloop.run()
    except KeyboardInterrupt:
        window.close()
        mainloop.quit()


if __name__ == '__main__':
    setup_logger(verbose='-v' in sys.argv)
    log.info("Loading Pyrasite...")
    sys.exit(main())

# vim: tabstop=4 shiftwidth=4 expandtab
