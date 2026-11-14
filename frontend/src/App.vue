<script setup>
import { computed, ref } from "vue";

const token = ref(localStorage.getItem("tide.token") || "");
const profile = ref(JSON.parse(localStorage.getItem("tide.profile") || "null"));
const username = ref("admin");
const password = ref("123456");
const loginError = ref("");
const batches = ref([]);
const current = ref(null);
const policy = ref(null);
const queue = ref([]);
const queueFilter = ref("");
const title = ref("");
const rawText = ref("");
const batchExit = ref("");
const policyExit = ref("");
const formError = ref("");
const policyError = ref("");
const queueError = ref("");
const busy = ref(false);

const canWrite = computed(() => profile.value && profile.value.role === "writer");
const policyLocked = computed(() => Boolean(policy.value && policy.value.lock_reason));
const pendingCount = computed(() => (policy.value ? policy.value.pending_count : 0));

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token.value) headers.Authorization = `Bearer ${token.value}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.detail || "请求没有完成";
    throw new Error(typeof message === "string" ? message : "请求没有完成");
  }
  return data;
}

async function refresh() {
  [batches.value, policy.value, queue.value] = await Promise.all([
    request("/api/batches"),
    request("/api/flag-policy"),
    request(`/api/quarantine${queueFilter.value ? `?status=${encodeURIComponent(queueFilter.value)}` : ""}`),
  ]);
  if (policy.value && !policyExit.value) {
    policyExit.value = policy.value.exit_code;
  }
  if (current.value) {
    await openBatch(current.value.batch.id);
  } else if (batches.value.length) {
    await openBatch(batches.value[0].id);
  }
}

async function openBatch(id) {
  current.value = await request(`/api/batches/${id}`);
}

async function refreshQueue() {
  queueError.value = "";
  try {
    queue.value = await request(
      `/api/quarantine${queueFilter.value ? `?status=${encodeURIComponent(queueFilter.value)}` : ""}`
    );
    policy.value = await request("/api/flag-policy");
  } catch (error) {
    queueError.value = error.message;
  }
}

async function submitLogin() {
  loginError.value = "";
  try {
    const data = await request("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    token.value = data.access_token;
    profile.value = { username: data.username, role: data.role, title: data.title };
    localStorage.setItem("tide.token", token.value);
    localStorage.setItem("tide.profile", JSON.stringify(profile.value));
    await refresh();
  } catch (error) {
    loginError.value = error.message;
  }
}

async function submitBatch() {
  formError.value = "";
  busy.value = true;
  try {
    const body = { title: title.value, raw_text: rawText.value };
    if (batchExit.value) body.suspect_exit = batchExit.value;
    const data = await request("/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    title.value = "";
    rawText.value = "";
    // 本批请求的出路只对这一批有效，交完收回默认，免得下一批悄悄带着旧偏离值。
    batchExit.value = "";
    await refresh();
    current.value = data;
  } catch (error) {
    formError.value = error.message;
  } finally {
    busy.value = false;
  }
}

async function savePolicy() {
  policyError.value = "";
  if (!policyExit.value) {
    policyError.value = "先选一条出路";
    return;
  }
  try {
    policy.value = await request("/api/flag-policy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exit_code: policyExit.value }),
    });
    await refresh();
  } catch (error) {
    policyError.value = error.message;
    // 服务端已经锁住或策略被别处改过：拉回真实状态，别让页面停在过期态。
    try {
      await refreshQueue();
      await refresh();
      if (policy.value) policyExit.value = policy.value.exit_code;
    } catch {
      /* 拉取失败就保留错误提示，下轮刷新再对齐 */
    }
  }
}

async function reviewRow(row) {
  queueError.value = "";
  try {
    await request(`/api/quarantine/${row.id}/review`, { method: "POST" });
    await refreshQueue();
    if (current.value) await openBatch(current.value.batch.id);
    batches.value = await request("/api/batches");
  } catch (error) {
    queueError.value = error.message;
  }
}

function logout() {
  token.value = "";
  profile.value = null;
  current.value = null;
  batches.value = [];
  queue.value = [];
  policy.value = null;
  localStorage.removeItem("tide.token");
  localStorage.removeItem("tide.profile");
}

if (token.value) {
  refresh().catch(() => logout());
}
</script>

<template>
  <header class="top">
    <div>
      <p class="eyebrow">水文组 · 报文归集</p>
      <h1>潮位报文归集台</h1>
    </div>
    <div v-if="profile" class="who">
      <span>{{ profile.title }} {{ profile.username }}</span>
      <button class="ghost" @click="logout">离开</button>
    </div>
  </header>

  <main v-if="!profile" class="gate">
    <form @submit.prevent="submitLogin">
      <p>站码、时刻、潮位厘米、质量旗。一行一条，竖线分开。</p>
      <label>用户<input v-model="username" autocomplete="username" /></label>
      <label>口令<input v-model="password" type="password" autocomplete="current-password" /></label>
      <button>进入归集台</button>
      <p v-if="loginError" class="warn">{{ loginError }}</p>
    </form>
  </main>

  <main v-else class="desk">
    <div class="rail">
      <section class="paste">
        <h2>贴入这一批</h2>
        <p class="rule">质量旗 A 实测，M 插补，B 可疑。可疑旗单独过一道理，不和实测混作一种落库。</p>
        <label>批次名<input v-model="title" :disabled="!canWrite" placeholder="例如 堡镇 11-13 午班" /></label>
        <textarea
          v-model="rawText"
          :disabled="!canWrite"
          placeholder="BZ03|202611130900|176|A"
          spellcheck="false"
        />
        <label v-if="canWrite">
          本批请求的可疑旗出路
          <select v-model="batchExit">
            <option value="">跟随当日策略</option>
            <option v-for="option in policy ? policy.options : []" :key="option.code" :value="option.code">
              {{ option.name }}
            </option>
          </select>
        </label>
        <button :disabled="!canWrite || busy" @click="submitBatch">归集这一批</button>
        <p v-if="!canWrite" class="quiet">档案员只能翻已落入的报文。</p>
        <p v-if="formError" class="warn">{{ formError }}</p>
      </section>

      <section class="policy">
        <header>
          <h2>旗设置</h2>
          <em v-if="policy">{{ policy.exit_name }}</em>
        </header>
        <p class="rule">
          可疑旗的出路写进策略栏后，本日不得更换。归集请求里的出路若和这里不一致，该行记“旗出路偏了”，不落库。
        </p>
        <p v-if="policy && policy.is_set" class="quiet">
          {{ policy.day }} 由 {{ policy.set_by }} 写入，今日生效：{{ policy.exit_name }}
        </p>
        <label v-if="canWrite">
          出路
          <select v-model="policyExit" :disabled="policyLocked">
            <option v-for="option in policy ? policy.options : []" :key="option.code" :value="option.code">
              {{ option.name }} · {{ option.summary }}
            </option>
          </select>
        </label>
        <button v-if="canWrite" :disabled="policyLocked" @click="savePolicy">写入策略栏</button>
        <p v-if="policy && policy.lock_reason" class="lock">{{ policy.lock_reason }}</p>
        <p v-if="policyError" class="warn">{{ policyError }}</p>
      </section>
    </div>

    <section class="slips">
      <h2>已收批次</h2>
      <div class="batch-list">
        <button
          v-for="batch in batches"
          :key="batch.id"
          class="slip"
          :class="{ on: current && current.batch.id === batch.id }"
          @click="openBatch(batch.id)"
        >
          <strong>{{ batch.title }}</strong>
          <em>{{ batch.status }}</em>
          <span>
            落入 {{ batch.accepted_count }} 行 · 未接纳 {{ batch.rejected_count }} 行 ·
            待隔离 {{ batch.pending_count }} 行 · 已复核 {{ batch.reviewed_count }} 行
          </span>
          <span class="meta">
            策略 {{ batch.policy_name }} · 本批 {{ batch.suspect_exit_name }}
            <b v-if="batch.suspect_exit_deviation" class="bad">· 请求偏离策略</b>
          </span>
          <span class="meta">
            可疑旗
            <b :class="batch.suspect_landed ? 'landed' : 'held'">
              {{ batch.suspect_landed ? "已入库" : "未入库" }}
            </b>
          </span>
        </button>
      </div>

      <article v-if="current" class="tape">
        <header>
          <h3>{{ current.batch.title }}</h3>
          <span>{{ current.batch.status }}</span>
        </header>
        <p class="trace">
          策略 {{ current.policy.exit_name }} · 本批按 {{ current.batch.suspect_exit_name }} 处理
          <b v-if="current.batch.suspect_exit_deviation" class="bad">· 有行请求偏离，已拦下</b>
        </p>

        <h4>已落库</h4>
        <table>
          <thead>
            <tr><th>行</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th><th>入库情形</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in current.landed" :key="row.line_no">
              <td>{{ row.line_no }}</td>
              <td>{{ row.station_code }}</td>
              <td>{{ row.observed_at }}</td>
              <td>{{ row.tide_cm }}</td>
              <td>{{ row.flag_label }}</td>
              <td>
                <b v-if="row.review_state" class="review">{{ row.review_label }}</b>
                <span v-else class="quiet">直接入库</span>
              </td>
            </tr>
            <tr v-if="!current.landed.length"><td colspan="6">这一批没有落入的行</td></tr>
          </tbody>
        </table>

        <h4>旗号详情</h4>
        <table>
          <thead>
            <tr><th>站</th><th>旗</th><th>入库</th><th>待复核</th><th>隔离队列</th><th>可疑旗状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="entry in current.flag_summary" :key="entry.station_code">
              <td>{{ entry.station_code }}</td>
              <td>{{ entry.flags.map((f) => f.flag_label).join("、") || "—" }}</td>
              <td>{{ entry.landed }}</td>
              <td>{{ entry.review }}</td>
              <td>
                {{ entry.quarantined }}（待隔离 {{ entry.quarantine_pending }} · 已复核
                {{ entry.quarantine_reviewed }}）
              </td>
              <td>
                <b v-if="entry.suspect_landed" class="landed">已入库，待复核</b>
                <b v-else-if="entry.suspect_state" class="held">{{ entry.suspect_state }}，未入库</b>
                <span v-else class="quiet">本批没有可疑旗</span>
              </td>
            </tr>
            <tr v-if="!current.flag_summary.length"><td colspan="6">这一批还没有任何汇入</td></tr>
          </tbody>
        </table>

        <h4>隔离队列</h4>
        <ul class="queue">
          <li v-for="row in current.quarantine" :key="row.id">
            <code>{{ row.raw_line }}</code>
            <span>{{ row.reason }}</span>
            <span class="quiet">
              {{ row.status }} · 由 {{ row.claimed_by }} 认领 · {{ row.claimed_at }}
            </span>
          </li>
          <li v-if="!current.quarantine.length" class="quiet">这一批没有可疑行进入隔离</li>
        </ul>

        <h4>未接纳</h4>
        <ul>
          <li v-for="row in current.rejected" :key="row.line_no">
            <code>{{ row.raw_line }}</code>
            <span>{{ row.reason }}</span>
          </li>
          <li v-if="!current.rejected.length">没有被挡下的行</li>
        </ul>
      </article>
    </section>

    <section class="quarantine">
      <header>
        <h2>隔离队列</h2>
        <div class="filters">
          <select v-model="queueFilter" @change="refreshQueue">
            <option value="">全部</option>
            <option value="待隔离">待隔离</option>
            <option value="已复核">已复核</option>
          </select>
          <span class="quiet">待隔离 {{ pendingCount }} 行</span>
        </div>
      </header>
      <p v-if="policyLocked && pendingCount" class="lock">
        队列尚未腾空，旗设置锁着：{{ policy.lock_reason }}
      </p>
      <table>
        <thead>
          <tr><th>批次</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th><th>隔离原因</th><th>认领</th><th>状态</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="row in queue" :key="row.id">
            <td>{{ row.batch_title }}</td>
            <td>{{ row.station_code }}</td>
            <td>{{ row.observed_at }}</td>
            <td>{{ row.tide_cm }}</td>
            <td>{{ row.flag_label }}</td>
            <td>{{ row.reason }}</td>
            <td>{{ row.claimed_by }}</td>
            <td>{{ row.status }}</td>
            <td>
              <button
                v-if="canWrite && row.status === '待隔离'"
                class="mini"
                @click="reviewRow(row)"
              >复核</button>
              <span v-else class="quiet">—</span>
            </td>
          </tr>
          <tr v-if="!queue.length"><td colspan="9">隔离队列是空的</td></tr>
        </tbody>
      </table>
      <p v-if="queueError" class="warn">{{ queueError }}</p>
    </section>
  </main>
</template>

<style scoped>
.top, .desk, .gate { width: min(1320px, calc(100% - 32px)); margin: 0 auto; }
.top { display: flex; justify-content: space-between; align-items: end; padding: 28px 0 12px; }
.eyebrow { letter-spacing: 0.18em; text-transform: uppercase; color: #d7a15f; font-size: 12px; margin: 0 0 6px; }
h1 { margin: 0; font-weight: 560; font-size: 34px; }
h2, h3, h4 { margin: 0; }
.who { display: flex; gap: 12px; align-items: center; }
.ghost { background: transparent; color: #f3efe2; border: 1px solid rgba(243, 239, 226, 0.35); padding: 6px 12px; }
.gate { padding: 24px 0 48px; }
.gate form, .paste, .policy, .tape, .quarantine {
  background: #f3efe2;
  color: #1c2430;
  padding: 22px;
}
.gate form { width: min(420px, 100%); display: grid; gap: 12px; }
label { display: grid; gap: 6px; font-size: 14px; }
input, textarea, select {
  width: 100%;
  border: 0;
  background: #fffdf8;
  color: #1c2430;
  padding: 10px 12px;
  box-shadow: inset 0 0 0 1px #d9d0bc;
}
textarea { min-height: 180px; font-family: "IBM Plex Mono", Consolas, monospace; line-height: 1.6; }
button { background: #9d2c2c; color: #fffaf2; padding: 10px 14px; }
button:disabled { opacity: 0.45; cursor: not-allowed; }
.mini { padding: 4px 10px; font-size: 13px; }
.desk { display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 22px; padding-bottom: 40px; align-items: start; }
.rail, .paste, .policy, .slips, .quarantine { display: grid; gap: 12px; align-content: start; }
.batch-list { display: grid; gap: 10px; }
.rule, .quiet { margin: 0; color: #5c5648; font-size: 14px; }
.policy header, .quarantine header, .filters { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.filters select { width: auto; padding: 6px 10px; }
.policy em { font-style: normal; color: #9d2c2c; font-size: 13px; }
.lock { margin: 0; color: #9d2c2c; font-size: 13px; }
.slip {
  text-align: left;
  background: transparent;
  color: #f3efe2;
  border: 1px solid rgba(243, 239, 226, 0.2);
  padding: 12px 14px;
  display: grid;
  gap: 4px;
}
.slip.on { border-color: #d7a15f; }
.slip em { font-style: normal; color: #d7a15f; }
.slip span, .tape li span, .meta { color: #5c5648; }
.slip .meta { font-size: 13px; }
.landed { color: #2f6b45; }
.held { color: #9d2c2c; }
.bad { color: #b4762a; }
.review { color: #b4762a; }
.tape header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.tape h4 { margin: 14px 0 8px; }
.trace { margin: 0 0 8px; font-size: 13px; color: #5c5648; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #e4dcc8; font-size: 14px; vertical-align: top; }
ul { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
li { display: grid; gap: 2px; }
.queue { gap: 10px; }
code { font-family: Consolas, monospace; }
.warn { color: #9d2c2c; margin: 0; }
.quarantine { margin-top: 4px; }
@media (max-width: 1100px) {
  .desk { grid-template-columns: 1fr; }
}
</style>
