// Live refresh: poll the JSON endpoints every 3 s and update the page. Text only (textContent), never HTML.
(function () {
  var jobEl = document.getElementById("job");
  var poolBody = document.getElementById("pool-body");
  var opts = { headers: { "Accept": "application/json" }, credentials: "same-origin" };

  function setField(name, value) {
    var el = document.querySelector('[data-field="' + name + '"]');
    if (el) el.textContent = (value === null || value === undefined || value === "") ? "-" : value;
  }

  function refreshJob() {
    fetch("/jobs/" + jobEl.dataset.jobId, opts).then(function (r) { return r.json(); }).then(function (j) {
      var s = document.querySelector('[data-field="state"]');
      if (s) { s.textContent = j.state; s.className = "badge " + j.state; }
      setField("gpu_name", j.gpu_name);
      setField("queue_position", j.queue_position);
      setField("result_note", j.result_note);
      var msg = "";
      if (j.state === "held") msg = "Waiting: " + (j.held_reason || "");
      else if (j.state === "queued") msg = "Waiting for the scheduler to place this job.";
      else if (j.state === "running") msg = "Running on " + (j.gpu_name || "a GPU") + (j.remaining_s !== null ? " - about " + j.remaining_s + " s left." : ".");
      else if (j.state === "completed") msg = "Finished.";
      setField("message", msg);
    }).catch(function () {});
  }

  function cell(tr, text, cls) {
    var td = document.createElement("td");
    if (cls) { var b = document.createElement("span"); b.className = "badge " + cls; b.textContent = text; td.appendChild(b); }
    else td.textContent = text;
    tr.appendChild(td);
  }

  function refreshPool() {
    fetch("/pool", opts).then(function (r) { return r.json(); }).then(function (p) {
      document.getElementById("queue").textContent = p.queue_length;
      poolBody.textContent = "";
      p.gpus.forEach(function (g) {
        var tr = document.createElement("tr");
        cell(tr, g.name + (g.simulated ? " (simulated)" : ""));
        cell(tr, g.memory_gb + " GB");
        var st = g.busy ? "busy" : (g.status === "removed" ? "removed" : "free");
        cell(tr, st, g.busy ? "running" : (g.status === "removed" ? "removed" : "free"));
        var who = g.busy ? (g.who + (g.job_id ? " - job #" + g.job_id : "") + (g.free_in_s !== null ? " - free in ~" + g.free_in_s + " s" : "")) : "-";
        cell(tr, who);
        cell(tr, g.util_pct !== null ? Math.round(g.util_pct) + "% - " + Math.round(g.mem_used_mb) + " MB" : "-");
        poolBody.appendChild(tr);
      });
    }).catch(function () {});
  }

  if (jobEl) { refreshJob(); setInterval(refreshJob, 3000); }
  if (poolBody) { setInterval(refreshPool, 3000); }
})();
