(function () {
  // Pantalla de "trabajando…" para acciones largas
  document.querySelectorAll('form[data-busy]').forEach(function (f) {
    f.addEventListener('submit', function () {
      var b = document.getElementById('busy');
      document.getElementById('busy-msg').textContent = f.dataset.busy;
      b.hidden = false;
      f.querySelectorAll('button').forEach(function (x) { x.disabled = true; });
    });
  });
  // Confirmar antes de borrar
  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });
  // Mostrar/ocultar secciones
  document.querySelectorAll('[data-toggle]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var el = document.getElementById(btn.dataset.toggle); el.hidden = !el.hidden;
      if (!el.hidden) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
  // Pestañas
  document.querySelectorAll('[data-tabs]').forEach(function (tabs) {
    tabs.querySelectorAll('[data-tab]').forEach(function (t) {
      t.addEventListener('click', function () {
        tabs.querySelectorAll('[data-tab]').forEach(function (o) {
          o.classList.toggle('on', o === t);
          document.getElementById(o.dataset.tab).hidden = o !== t;
        });
      });
    });
  });
  // Copiar
  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var el = document.getElementById(btn.dataset.copy);
      var txt = el.value || el.textContent;
      var done = function () { var o = btn.textContent; btn.textContent = '¡Copiado! ✓'; setTimeout(function () { btn.textContent = o; }, 1500); };
      if (navigator.clipboard) navigator.clipboard.writeText(txt).then(done, function () { el.select(); document.execCommand('copy'); done(); });
      else { el.select(); document.execCommand('copy'); done(); }
    });
  });
  // Enviar al elegir en un select
  document.querySelectorAll('select[data-autosubmit]').forEach(function (s) {
    s.addEventListener('change', function () { if (s.value) s.form.submit(); });
  });
  // Seguir el progreso de la búsqueda
  var st = document.querySelector('[data-poll]');
  if (st) {
    var tick = function () {
      fetch('/api/estado-busqueda', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.running) {
          var last = (d.log || []).slice(-1)[0];
          st.textContent = 'Buscando vacantes… ' + (last ? '(' + last + ')' : '');
          setTimeout(tick, 5000);
        } else { location.reload(); }
      }).catch(function () { setTimeout(tick, 10000); });
    };
    setTimeout(tick, 4000);
  }
})();
