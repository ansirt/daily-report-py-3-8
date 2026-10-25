const { createApp } = Vue;

async function api(method, url, body) {
  const r = await fetch(url, {
    method, headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) {
    if (r.status === 401 && !url.startsWith('/api/auth/login')) app.user = null;
    let d = data && data.detail;
    if (Array.isArray(d)) d = d.map(x => (x.loc || []).slice(1).join('.') + ': ' + x.msg).join('; ');
    throw new Error(d || r.statusText);
  }
  return data;
}

const app = createApp({
  data() {
    return {
      user: null, view: 'jobs', busy: false, error: '', notice: '',
      menu: [{ id: 'jobs', label: 'Cronjob' }, { id: 'runs', label: 'Riwayat run' }, { id: 'files', label: 'File' }, { id: 'conns', label: 'Koneksi database' },
             { id: 'smtp', label: 'Email / SMTP' }, { id: 'account', label: 'Akun' }],
      kindText: { py_script: 'skrip python', db_dump: 'dump DB', sql: 'get data', stage: 'staging', join: 'join', merge: 'join', compare: 'bandingkan', pivot: 'pivot', db_write: 'tulis DB', api_get: 'API', csv_export: 'export CSV', csv_import: 'import CSV' },
      statusText: { running: 'Berjalan', success: 'Berhasil', failed: 'Gagal' },
      loginForm: { username: '', password: '' }, loginError: '',
      jobs: [], conns: [], runs: [], runFilter: 0, jf: null, cf: null, sf: null, csvFiles: [], files: [], features: null, dumpCatalog: {},
      runDlg: null, runDetail: null, testTo: '', pw: { old_password: '', new_password: '' }, timer: null,
    };
  },
  async mounted() {
    try { this.user = (await api('GET', '/api/auth/me')).username; await this.afterLogin(); } catch (e) { this.user = null; }
    this.timer = setInterval(() => this.poll(), 4000);
  },
  methods: {
    fmt(s) { return s ? s.replace('T', ' ') : ''; },
    async guard(fn) {
      this.busy = true; this.error = '';
      try { return await fn(); } catch (e) { this.error = e.message; } finally { this.busy = false; }
    },
    flash(msg) { this.notice = msg; setTimeout(() => { if (this.notice === msg) this.notice = ''; }, 5000); },
    async login() {
      this.busy = true; this.loginError = '';
      try { this.user = (await api('POST', '/api/auth/login', this.loginForm)).username; this.loginForm.password = ''; await this.afterLogin(); }
      catch (e) { this.loginError = e.message; } finally { this.busy = false; }
    },
    openHelp() { window.open('/api/help', '_blank');},
    async logout() { await api('POST', '/api/auth/logout', {}); this.user = null; },
    async afterLogin() { this.go('jobs'); },
    async go(v) {
      this.error = ''; this.notice = ''; this.view = v;
      if (v === 'jobs') { await this.guard(async () => { this.jobs = await api('GET', '/api/jobs'); }); }
      if (v === 'files') { await this.guard(async () => { this.files = await api('GET', '/api/files'); }); }
      if (v === 'runs') { if (!this.jobs.length) this.jobs = await api('GET', '/api/jobs'); await this.loadRuns(); }
      if (v === 'conns') { await this.loadConns(); }
      if (v === 'smtp') { await this.guard(async () => { this.sf = { ...(await api('GET', '/api/settings')), smtp_password: '' }; }); }
    },
    async poll() {
      if (!this.user || this.runDlg) return;
      try {
        if (this.view === 'jobs') this.jobs = await api('GET', '/api/jobs');
        else if (this.view === 'runs' && !this.runDetail) await this.loadRuns();
        if (this.runDetail && this.runDetail.status === 'running') this.runDetail = await api('GET', '/api/runs/' + this.runDetail.id);
      } catch (e) { /* diam */ }
    },
    // ---- jobs
    async toggleJob(j) {
      await this.guard(async () => {
        const full = await api('GET', '/api/jobs/' + j.id);
        await api('PUT', '/api/jobs/' + j.id, { ...full, enabled: !j.enabled });
        this.jobs = await api('GET', '/api/jobs');
      });
    },
    async openJob(id) {
      await this.guard(async () => {
        if (!this.conns.length) this.conns = await api('GET', '/api/connections');
        this.csvFiles = await api('GET', '/api/csv');
        this.dumpCatalog = await api('GET', '/api/dump/catalog');
        this.features = await api('GET', '/api/features');
        this.jf = id ? await api('GET', '/api/jobs/' + id) : {
          id: null, key: '', name: '', description: '', enabled: false, cron: '0 7 * * *', date_mode: 'today', layout: 'standard',
          email_enabled: true, email_to: '', email_cc: '', subject: '', filename_tpl: 'report_{tgl}', queries: [],
          body_html: '<html><body><p>Dear All,</p><p>Berikut laporan {hari}, {tgl_long}</p><p>Terima kasih atas perhatian dan kerja samanya</p></body></html>',
        };
        for (const q of this.jf.queries) {
          if (q.kind === 'pivot' && !q.config.values) q.config.values = [{ column: q.config.value || '', agg: q.config.agg || 'sum' }];
          if (q.kind === 'py_script' && !q.config.inputs) q.config.inputs = [];
          if (q.kind === 'db_dump') this.dumpDefaults(q);
        }
        this.view = 'job';
      });
    },
    addQuery() {
      this.jf.queries.push({ id: null, name: '', sheet_name: 'Sheet' + (this.jf.queries.length + 1), connection_id: null, sql: '',
        is_output: true, param_mode: 'none', param_source_position: null, param_source_column: '', kind: 'sql', config: {} });
    },
    kindChanged(q) {
      const d = {
        sql: {}, stage: { target_connection_id: null, table: 'STG_' },
        join: { left: null, right: null, left_on: '', right_on: '', left_cols: '', right_cols: '', join_type: 'left', output: 'single', sheet_extra: false, max_rows: 100000 },
        api_get: { url: '', method: 'GET', auth: 'none', token: '', basic_user: '', password: '', apikey_name: '', apikey_value: '', headers: '', body: '', records_path: '', columns: '', pagination: 'none', page_size: 100, start_page: 1, next_path: '', max_pages: 100, max_rows: 100000, verify_ssl: true },
        csv_export: { source: null, filename: '', delimiter: ',', encoding: 'utf-8', header: true },
        csv_import: { source: null, file: '', delimiter: ',', encoding: 'utf-8', header: true, types: '', empty_as_null: true },
        db_write: { source: null, target_connection_id: null, mode: 'insert', table: '', set: '', keys: '', max_rows: 10000 },
          pivot: { source: null, index: '', column: '', values: [{ column: '', agg: 'sum' }], total: true, max_rows: 1000 },
        py_script: { inputs: [null], extra_ports: '', timeout: 120, max_rows: 500000, sheet_extra: false,
          script: '# df = input pertama (DataFrame); inputs = semua input; params = tanggal (tgl_awal, tgl_akhir, ...)\n# Wajib isi `result` dengan DataFrame. Opsional: extra = {"nama": DataFrame} untuk port tambahan.\nresult = df\n' },
        db_dump: { filename: 'dump_{tgl}', compress: true, timeout: 3600, tables: '', options: {} },
      };
      q.config = d[q.kind];
      if (q.kind === 'db_dump') this.dumpDefaults(q);
      if (['join', 'pivot', 'db_write', 'api_get', 'csv_export', 'csv_import', 'py_script', 'db_dump'].includes(q.kind)) {
        if (q.kind === 'csv_export' || q.kind === 'csv_import') q.is_output = false; q.param_mode = 'none'; q.sql = ''; q.connection_id = null; }
    },
    async previewApi(q) {
      q._loading = true; q._error = ''; q._preview = null;
      try { q._preview = await api('POST', '/api/api-preview', { config: q.config, query_id: q.id, date_mode: this.jf.date_mode }); }
      catch (e) { q._error = e.message; } finally { q._loading = false; }
    },
    async uploadCsv(ev, q) {
      const f = ev.target.files[0];
      if (!f) return;
      await this.guard(async () => {
        const r = await fetch('/api/csv/' + encodeURIComponent(f.name), { method: 'PUT', body: f });
        if (!r.ok) { let d = null; try { d = (await r.json()).detail; } catch (e) { /* kosong */ } throw new Error(d || 'Unggah gagal'); }
        const res = await r.json();
        this.csvFiles = await api('GET', '/api/csv');
        q.config.file = res.path; q.config.source = null;
        this.flash('File ' + res.path + ' diunggah');
      });
      ev.target.value = '';
    },
    portNames(s) { return ((s.config && s.config.extra_ports) || '').split(/[,\s]+/).filter(Boolean); },
    dumpType(q) { const c = this.conns.find(x => x.id === q.connection_id); return c ? c.db_type : null; },
    dumpInfo(q) { return this.dumpCatalog[this.dumpType(q)] || null; },
    dumpDefaults(q) {
      const info = this.dumpInfo(q);
      q.config.options = q.config.options || {};
      if (!info) return;
      for (const o of info.options) {
        if (q.config.options[o.key] === undefined) q.config.options[o.key] = o.default !== null && o.default !== undefined ? o.default : (o.type === 'flag' ? false : '');
      }
    },
    tableList(q) { return (q.config.tables || '').split(/[\n,;]+/).map(x => x.trim()).filter(Boolean); },
    hasTable(q, t) { return this.tableList(q).includes(t); },
    toggleTable(q, t) {
      const l = this.tableList(q), i = l.indexOf(t);
      i >= 0 ? l.splice(i, 1) : l.push(t);
      q.config.tables = l.join('\n');
    },
    filteredTables(q) { const f = (q._filter || '').toLowerCase(); return (q._tables || []).filter(t => t.toLowerCase().includes(f)).slice(0, 400); },
    async loadTables(q) {
      q._loading = true; q._error = '';
      try { q._tables = await api('GET', '/api/connections/' + q.connection_id + '/tables'); } catch (e) { q._error = e.message; } finally { q._loading = false; }
    },
    async previewDump(q) {
      q._loading = true; q._error = ''; q._cmd = '';
      try { q._cmd = (await api('POST', '/api/dump/preview', { connection_id: q.connection_id, config: q.config, job_key: this.jf.key || 'job' })).command; }
      catch (e) { q._error = e.message; } finally { q._loading = false; }
    },
    async deleteFile(f) {
      if (!confirm('Hapus file ' + f.path + '?')) return;
      await this.guard(async () => { await api('DELETE', '/api/files?path=' + encodeURIComponent(f.path)); this.files = await api('GET', '/api/files'); });
    },
    colsOf(ref) {
      if (ref === null || ref === undefined || ref === '') return null;
      const n = this.jf.queries[parseInt(String(ref), 10)];
      return n && n._preview ? n._preview.columns : null;
    },
    moveQuery(i, d) {
      const q = this.jf.queries, j = i + d;
      if (j < 0 || j >= q.length) return;
      [q[i], q[j]] = [q[j], q[i]];
      if (q.some(x => x.param_mode === 'list' || ['join', 'pivot', 'db_write', 'csv_export', 'csv_import', 'py_script'].includes(x.kind))) this.notice = 'Urutan langkah berubah: periksa ulang pilihan sumber (#nomor) pada parameter daftar / gabung / bandingkan / pivot.';
    },
    async saveJob() {
      await this.guard(async () => {
        const body = { ...this.jf, queries: this.jf.queries.map(q => ({
          id: q.id, name: q.name, sheet_name: q.sheet_name, connection_id: q.connection_id, sql: q.sql, is_output: q.is_output,
          kind: q.kind, config: q.config, param_mode: q.param_mode, param_source_position: q.param_mode === 'list' ? q.param_source_position : null,
          param_source_column: q.param_mode === 'list' ? q.param_source_column : null })) };
        this.jf = this.jf.id ? await api('PUT', '/api/jobs/' + this.jf.id, body) : await api('POST', '/api/jobs', body);
        this.flash('Job disimpan');
      });
    },
    async deleteJob() {
      if (!confirm('Hapus job "' + this.jf.name + '" beserta riwayatnya?')) return;
      await this.guard(async () => { await api('DELETE', '/api/jobs/' + this.jf.id); await this.go('jobs'); });
    },
    async previewQuery(q) {
      q._loading = true; q._error = ''; q._preview = null;
      try { q._preview = await api('POST', '/api/queries/preview', { connection_id: q.connection_id, sql: q.sql, param_mode: q.param_mode, date_mode: this.jf.date_mode }); }
      catch (e) { q._error = e.message; } finally { q._loading = false; }
    },
    async runJob() {
      const { job, send_email, dry_run, date_from, date_to } = this.runDlg;
      if (!!date_from !== !!date_to) { this.error = 'Isi tanggal awal dan tanggal akhir sekaligus, atau kosongkan keduanya.'; return; }
      const dry = !!(job.writes && dry_run);
      if (job.writes && !dry && !confirm('Job ini akan MENULIS ke database. Lanjutkan?')) return;
      await this.guard(async () => {
        const r = await api('POST', '/api/jobs/' + job.id + '/run', { send_email: dry ? false : send_email, dry_run: dry, date_from: date_from || null, date_to: date_to || null });
        this.runDlg = null; this.flash((dry ? 'Dry-run' : 'Job') + ' dijalankan (run #' + r.run_id + ')');
        this.jobs = await api('GET', '/api/jobs');
      });
    },
    // ---- runs
    async openRuns(id) { this.runFilter = id; this.view = 'runs'; if (!this.jobs.length) this.jobs = await api('GET', '/api/jobs'); await this.loadRuns(); },
    async loadRuns() { this.runs = await api('GET', '/api/runs?limit=200' + (this.runFilter ? '&job_id=' + this.runFilter : '')); },
    async openRun(id) { await this.guard(async () => { this.runDetail = await api('GET', '/api/runs/' + id); }); },
    async resend() {
      await this.guard(async () => { await api('POST', '/api/runs/' + this.runDetail.id + '/resend', {}); this.runDetail = await api('GET', '/api/runs/' + this.runDetail.id); this.flash('Email terkirim'); });
    },
    // ---- connections
    newConn() { return { id: null, name: '', db_type: 'oracle', host: '', port: null, database: '', username: '', password: '', dsn: '', note: '', allow_write: false }; },
    async loadConns() { await this.guard(async () => { this.conns = await api('GET', '/api/connections'); }); },
    async saveConn() {
      await this.guard(async () => {
        const b = { ...this.cf, port: this.cf.port || null };
        await (this.cf.id ? api('PUT', '/api/connections/' + this.cf.id, b) : api('POST', '/api/connections', b));
        this.cf = null; this.conns = await api('GET', '/api/connections'); this.flash('Koneksi disimpan');
      });
    },
    async deleteConn(c) { if (confirm('Hapus koneksi "' + c.name + '"?')) await this.guard(async () => { await api('DELETE', '/api/connections/' + c.id); this.conns = await api('GET', '/api/connections'); }); },
    async testConn(c) {
      await this.guard(async () => { const r = await api('POST', '/api/connections/' + c.id + '/test', {}); r.ok ? this.flash(c.name + ': ' + r.message) : (this.error = c.name + ': ' + r.message); });
    },
    // ---- smtp & account
    async saveSmtp() { await this.guard(async () => { this.sf = { ...(await api('PUT', '/api/settings', this.sf)), smtp_password: '' }; this.flash('Pengaturan email disimpan'); }); },
    async testMail() { await this.guard(async () => { const r = await api('POST', '/api/settings/test-email', { to: this.testTo }); r.ok ? this.flash(r.message) : (this.error = r.message); }); },
    async changePassword() {
      await this.guard(async () => { await api('POST', '/api/auth/change-password', this.pw); this.pw = { old_password: '', new_password: '' }; this.flash('Password diganti'); });
    },
  },
}).mount('#app');
