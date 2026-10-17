const KEY = "exam_sel";

function getSel() {
  try { return JSON.parse(localStorage.getItem(KEY) || "[]"); }
  catch (e) { return []; }
}
function setSel(a) { localStorage.setItem(KEY, JSON.stringify(a)); render(); }

function render() {
  const s = getSel();
  const n = document.getElementById("tray-n");
  if (n) n.textContent = s.length;
  document.querySelectorAll(".sel").forEach(cb => {
    cb.checked = s.includes(+cb.dataset.id);
  });
}

document.addEventListener("change", e => {
  if (!e.target.classList.contains("sel")) return;
  const id = +e.target.dataset.id;
  let s = getSel();
  if (e.target.checked) { if (!s.includes(id)) s.push(id); }
  else { s = s.filter(x => x !== id); }
  setSel(s);
});

const go = document.getElementById("tray-go");
if (go) go.onclick = () => { location = "/compose?ids=" + getSel().join(","); };
const clr = document.getElementById("tray-clear");
if (clr) clr.onclick = () => setSel([]);

render();
