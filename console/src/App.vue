<script setup>
import { computed, onMounted, onUnmounted, shallowRef } from "vue";
import { call } from "./api.js";

const health = shallowRef(null);
const rooms = shallowRef([]);
const events = shallowRef([]);
const selected = shallowRef(null);
const statusFilter = shallowRef("Live");
const eventTypeFilter = shallowRef("");
const eventTableFilter = shallowRef("");
const loading = shallowRef(false);
const error = shallowRef("");
const stopping = shallowRef("");
const confirmStopId = shallowRef("");

let timer = null;

const graceLabel = computed(() => {
  if (!health.value) return "";
  const kill = health.value.kill_challenge_grace_seconds;
  if (kill) return `kill ${kill}s`;
  return `${health.value.grace_seconds}s grace`;
});

async function refreshHealth() {
  health.value = await call("domino_stream.api.console.get_health");
}

async function refreshRooms() {
  const status = statusFilter.value || undefined;
  const res = await call("domino_stream.api.console.list_rooms", {
    status: status || null,
    limit: 80,
  });
  rooms.value = res.rooms || [];
}

async function refreshEvents() {
  const res = await call("domino_stream.api.console.list_events", {
    table_id: eventTableFilter.value || null,
    event_type: eventTypeFilter.value || null,
    limit: 80,
  });
  events.value = res.events || [];
}

async function refreshAll() {
  loading.value = true;
  error.value = "";
  try {
    await Promise.all([refreshHealth(), refreshRooms(), refreshEvents()]);
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    loading.value = false;
  }
}

async function openRoom(row) {
  error.value = "";
  try {
    selected.value = await call("domino_stream.api.console.get_room", {
      table_id: row.table_id,
    });
    eventTableFilter.value = row.table_id;
    await refreshEvents();
  } catch (e) {
    error.value = e.message || String(e);
  }
}

async function forceStop(tableId) {
  if (!tableId) return;
  confirmStopId.value = tableId;
}

async function confirmForceStop() {
  const tableId = confirmStopId.value;
  if (!tableId) return;
  confirmStopId.value = "";
  stopping.value = tableId;
  error.value = "";
  try {
    await call("domino_stream.api.console.force_stop", { table_id: tableId });
    selected.value = null;
    await refreshAll();
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    stopping.value = "";
  }
}

function fmtAge(sec) {
  if (sec == null) return "—";
  if (sec < 60) return `${Math.round(sec)}s`;
  return `${Math.round(sec / 60)}m`;
}

function sevClass(sev) {
  if (sev === "Error") return "sev-error";
  if (sev === "Warning") return "sev-warn";
  return "sev-info";
}

onMounted(() => {
  refreshAll();
  timer = setInterval(refreshAll, 10000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="shell">
    <header class="top">
      <div>
        <h1>Domino Stream Console</h1>
        <p class="sub">
          Control plane ops · shared DCMS session
          <span v-if="health">· {{ health.user }}</span>
        </p>
      </div>
      <div class="top-actions">
        <a
          v-if="health?.stream_settings_url"
          class="link"
          :href="health.stream_settings_url"
          target="_blank"
          rel="noopener"
          >Stream Settings</a
        >
        <button type="button" class="btn" :disabled="loading" @click="refreshAll">
          {{ loading ? "Refreshing…" : "Refresh" }}
        </button>
      </div>
    </header>

    <p v-if="error" class="banner err">{{ error }}</p>

    <div v-if="confirmStopId" class="banner warn confirm-bar">
      <span>Force stop <strong class="mono">{{ confirmStopId }}</strong>?</span>
      <div class="top-actions">
        <button type="button" class="btn" @click="confirmStopId = ''">Cancel</button>
        <button type="button" class="btn danger" @click="confirmForceStop">Force stop</button>
      </div>
    </div>

    <section v-if="health" class="health">
      <div class="chip" :class="health.sfu_configured ? 'ok' : 'bad'">
        SFU {{ health.sfu_configured ? "ready" : "not configured" }}
      </div>
      <div class="chip" title="Presence offline → kill_challenge → RQ delayed force-stop">
        Force-kill RQ
      </div>
      <div
        class="chip"
        :title="
          health.reconcile_required
            ? 'Legacy reconcile'
            : 'Legacy reconcile not required (kill_challenge owns abandon)'
        "
      >
        Reconcile
        {{
          health.reconcile_job?.stopped || health.reconcile_paused_config
            ? "off"
            : "legacy-on"
        }}
      </div>
      <div class="chip">{{ graceLabel }}</div>
      <div class="chip accent">{{ health.live_room_count }} live</div>
    </section>

    <div class="grid">
      <section class="panel">
        <div class="panel-head">
          <h2>Rooms</h2>
          <select v-model="statusFilter" @change="refreshRooms">
            <option value="">All</option>
            <option value="Live">Live</option>
            <option value="Stopped">Stopped</option>
            <option value="Preparing">Preparing</option>
            <option value="Error">Error</option>
          </select>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Table</th>
                <th>Status</th>
                <th>HB age</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="r in rooms"
                :key="r.name"
                :class="{ active: selected?.table_id === r.table_id }"
                @click="openRoom(r)"
              >
                <td>
                  <div class="mono">{{ r.table_id }}</div>
                  <div class="muted tiny">{{ r.publisher_user || "—" }}</div>
                </td>
                <td>
                  <span class="pill" :data-status="r.status">{{ r.status }}</span>
                </td>
                <td>
                  <span :class="{ stale: r.status === 'Live' && !r.heartbeat_fresh }">
                    {{ fmtAge(r.hb_age_s) }}
                  </span>
                </td>
                <td>
                  <button
                    v-if="r.status === 'Live'"
                    type="button"
                    class="btn danger sm"
                    :disabled="stopping === r.table_id"
                    @click.stop="forceStop(r.table_id)"
                  >
                    Stop
                  </button>
                </td>
              </tr>
              <tr v-if="!rooms.length">
                <td colspan="4" class="muted">No rooms</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="panel detail">
        <div class="panel-head">
          <h2>Room detail</h2>
        </div>
        <div v-if="!selected" class="muted pad">Select a room</div>
        <div v-else class="pad detail-body">
          <div class="kv">
            <span>Table</span><strong class="mono">{{ selected.table_id }}</strong>
          </div>
          <div class="kv">
            <span>Status</span><strong>{{ selected.status }}</strong>
          </div>
          <div class="kv">
            <span>Session</span
            ><strong class="mono tiny">{{ selected.publisher_session_id || "—" }}</strong>
          </div>
          <div class="kv">
            <span>SFU verdict</span><strong>{{ selected.session_verdict }}</strong>
          </div>
          <div class="kv">
            <span>HB age</span><strong>{{ fmtAge(selected.hb_age_s) }}</strong>
          </div>
          <div class="kv">
            <span>Tracks</span>
            <strong class="mono tiny"
              >{{ selected.video_track_name || "—" }} / {{ selected.audio_track_name || "—" }}
              (mid {{ selected.video_mid ?? "?" }}/{{ selected.audio_mid ?? "?" }})</strong
            >
          </div>
          <div class="kv block">
            <span>Last error</span>
            <pre>{{ selected.last_error || "—" }}</pre>
          </div>
          <div class="kv block">
            <span>SFU session</span>
            <pre>{{ JSON.stringify(selected.sfu_session, null, 2) }}</pre>
          </div>
          <button
            v-if="selected.status === 'Live'"
            type="button"
            class="btn danger"
            :disabled="stopping === selected.table_id"
            @click="forceStop(selected.table_id)"
          >
            Force stop
          </button>
        </div>
      </section>
    </div>

    <section class="panel log">
      <div class="panel-head">
        <h2>Event log</h2>
        <div class="filters">
          <input
            v-model="eventTableFilter"
            type="text"
            placeholder="table_id"
            @change="refreshEvents"
          />
          <select v-model="eventTypeFilter" @change="refreshEvents">
            <option value="">All types</option>
            <option value="stop">stop</option>
            <option value="publish">publish</option>
            <option value="reconcile">reconcile</option>
            <option value="sfu_error">sfu_error</option>
            <option value="console">console</option>
            <option value="heartbeat">heartbeat</option>
          </select>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Type</th>
              <th>Table</th>
              <th>Message</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in events" :key="e.name">
              <td class="tiny muted">{{ e.creation }}</td>
              <td>
                <span class="pill" :class="sevClass(e.severity)">{{ e.event_type }}</span>
              </td>
              <td class="mono tiny">{{ e.table_id || "—" }}</td>
              <td>{{ e.message }}</td>
            </tr>
            <tr v-if="!events.length">
              <td colspan="4" class="muted">No events yet</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.shell {
  max-width: 1280px;
  margin: 0 auto;
  padding: 1.25rem 1.5rem 3rem;
}
.top {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: flex-start;
  margin-bottom: 1rem;
}
h1 {
  margin: 0;
  font-size: 1.5rem;
  font-weight: 650;
  letter-spacing: -0.02em;
}
.sub {
  margin: 0.25rem 0 0;
  color: var(--muted);
  font-size: 0.9rem;
}
.top-actions {
  display: flex;
  gap: 0.75rem;
  align-items: center;
}
.link {
  color: var(--accent);
  text-decoration: none;
}
.btn {
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.45rem 0.85rem;
}
.btn:hover {
  border-color: var(--accent);
}
.btn.danger {
  background: #3a1f24;
  border-color: #6b3038;
  color: #ffb4b4;
}
.btn.sm {
  padding: 0.25rem 0.55rem;
  font-size: 0.8rem;
}
.banner {
  padding: 0.75rem 1rem;
  border-radius: 8px;
  margin-bottom: 1rem;
}
.banner.err {
  background: #3a1f24;
  color: #ffb4b4;
}
.banner.warn {
  background: #3a321f;
  color: #ffd89a;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
}
.health {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 1rem;
}
.chip {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.35rem 0.75rem;
  font-size: 0.85rem;
}
.chip.ok {
  border-color: #2f6b4c;
  color: var(--ok);
}
.chip.bad {
  border-color: #6b3038;
  color: var(--danger);
}
.chip.accent {
  border-color: #2a5578;
  color: var(--accent);
}
.grid {
  display: grid;
  grid-template-columns: 1.1fr 0.9fr;
  gap: 1rem;
  margin-bottom: 1rem;
}
@media (max-width: 900px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.75rem;
  padding: 0.85rem 1rem;
  border-bottom: 1px solid var(--border);
}
.panel-head h2 {
  margin: 0;
  font-size: 1rem;
}
.panel-head select,
.filters input,
.filters select {
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.35rem 0.5rem;
}
.filters {
  display: flex;
  gap: 0.5rem;
}
.table-wrap {
  overflow: auto;
  max-height: 420px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
}
th,
td {
  text-align: left;
  padding: 0.55rem 0.85rem;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
th {
  color: var(--muted);
  font-weight: 500;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
tbody tr {
  cursor: pointer;
}
tbody tr:hover,
tbody tr.active {
  background: var(--panel-2);
}
.mono {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
}
.tiny {
  font-size: 0.75rem;
}
.muted {
  color: var(--muted);
}
.stale {
  color: var(--warn);
}
.pill {
  display: inline-block;
  padding: 0.15rem 0.45rem;
  border-radius: 6px;
  background: var(--panel-2);
  font-size: 0.75rem;
}
.pill[data-status="Live"] {
  color: var(--ok);
}
.pill[data-status="Stopped"] {
  color: var(--muted);
}
.sev-error {
  color: var(--danger);
}
.sev-warn {
  color: var(--warn);
}
.sev-info {
  color: var(--accent);
}
.pad {
  padding: 1rem;
}
.detail-body .kv {
  display: grid;
  grid-template-columns: 7rem 1fr;
  gap: 0.35rem 0.75rem;
  margin-bottom: 0.55rem;
  font-size: 0.9rem;
}
.detail-body .kv span {
  color: var(--muted);
}
.detail-body .kv.block {
  display: block;
}
.detail-body pre {
  margin: 0.35rem 0 0.75rem;
  padding: 0.65rem;
  background: var(--bg);
  border-radius: 8px;
  overflow: auto;
  max-height: 180px;
  font-size: 0.75rem;
  white-space: pre-wrap;
  word-break: break-word;
}
.log .table-wrap {
  max-height: 360px;
}
</style>
