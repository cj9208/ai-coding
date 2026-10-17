function sync() {
  const ids = [...document.querySelectorAll("#pick-list li")].map(li => li.dataset.id);
  document.getElementById("ids-input").value = ids.join(",");
}

document.querySelectorAll(".mv").forEach(b => {
  b.onclick = () => {
    const li = b.closest("li");
    const list = li.parentElement;
    const dir = +b.dataset.dir;
    if (dir < 0 && li.previousElementSibling) list.insertBefore(li, li.previousElementSibling);
    if (dir > 0 && li.nextElementSibling) list.insertBefore(li.nextElementSibling, li);
    sync();
  };
});
document.querySelectorAll(".rm").forEach(b => {
  b.onclick = () => { b.closest("li").remove(); sync(); };
});
