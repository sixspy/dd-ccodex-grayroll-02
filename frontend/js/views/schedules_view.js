import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const SchedulesView = {
  setup() {
    const items = ref([]);
    const contention = ref([]);
    const form = ref({
      flag_id: 5, env_id: 1,
      steps_json: '[{"percent":10,"after_hours":0},{"percent":30,"after_hours":24},{"percent":50,"after_hours":48}]',
      note: "批量推送爬坡",
    });
    const err = ref(""); const msg = ref("");
    async function reload() {
      items.value = (await api("/api/schedules")).items || [];
      contention.value = (await api("/api/locks/contention?limit=20")).items || [];
    }
    async function create() {
      err.value = ""; msg.value = "";
      try { await api("/api/schedules", { method: "POST", body: JSON.stringify(form.value) }); msg.value = "已登记爬坡"; await reload(); }
      catch (e) { err.value = e.message || String(e); }
    }
    async function applyNext(id) {
      err.value = ""; msg.value = "";
      try {
        const r = await api(`/api/schedules/${id}/apply-next`, { method: "POST", body: "{}" });
        msg.value = `已推进 ${r.baseline_percent}% → ${r.applied_percent}%（步长 ${r.delta}，v${r.version}）`;
        await reload();
      } catch (e) { err.value = e.message || String(e); await reload(); }
    }
    onMounted(reload);
    return { items, contention, form, err, msg, create, applyNext, reload };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>比例爬坡计划</h2>
      <p class="hint">单步增量上限 20（取舍段）。超步整笔拒写，正式规则册零增、不落影子草案。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="form.flag_id" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="form.env_id" /></div>
      <div class="field"><label>步骤 JSON</label><textarea v-model="form.steps_json" rows="4"></textarea></div>
      <div class="field"><label>备注</label><input v-model="form.note" /></div>
      <button class="btn" @click="create">登记计划</button>
      <p class="muted" v-if="msg">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
      <h2 style="margin-top:1.1rem">最近争用记录</h2>
      <p class="hint">争用段（行锁/争用痕）只记谁没抢到锁，与步长取舍分开。</p>
      <table v-if="contention.length">
        <thead><tr><th>锁键</th><th>败方</th><th>胜方</th></tr></thead>
        <tbody>
          <tr v-for="c in contention" :key="c.id">
            <td><code>{{ c.lock_key }}</code></td><td>{{ c.loser }}</td><td>{{ c.winner }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted" v-else>暂无争用</p>
    </div>
    <div class="panel span-7">
      <h2>计划列表</h2>
      <p class="hint">推进一步 = 以正式规则为基线联检步长，同事务升版本并落发布册。</p>
      <table>
        <thead><tr><th>ID</th><th>开关</th><th>环境</th><th>状态</th><th>游标</th><th></th></tr></thead>
        <tbody>
          <tr v-for="it in items" :key="it.id">
            <td>{{ it.id }}</td><td>{{ it.flag_key }}</td><td>{{ it.env_code }}</td>
            <td>{{ it.status }}</td><td>{{ it.cursor_step }}</td>
            <td><button class="btn secondary" v-if="it.status==='planned' || it.status==='running'" @click="applyNext(it.id)">推进一步</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>`
};
