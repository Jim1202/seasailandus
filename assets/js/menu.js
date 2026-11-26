// Site menu: "Menu" button on phones, click-to-open Archive everywhere.
(function () {
  var header = document.querySelector(".site-header");
  if (!header) return;
  var menuBtn = header.querySelector(".menu-toggle");
  var archive = header.querySelector(".archive-dropdown");
  var archiveBtn = header.querySelector(".archive-toggle");

  function setMenu(open) {
    header.classList.toggle("nav-open", open);
    if (menuBtn) menuBtn.setAttribute("aria-expanded", String(open));
  }
  function setArchive(open) {
    if (!archive) return;
    archive.classList.toggle("is-open", open);
    archiveBtn.setAttribute("aria-expanded", String(open));
  }

  if (menuBtn) menuBtn.addEventListener("click", function () {
    setMenu(!header.classList.contains("nav-open"));
  });
  if (archiveBtn) archiveBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    setArchive(!archive.classList.contains("is-open"));
  });
  document.addEventListener("click", function (e) {
    if (archive && !archive.contains(e.target)) setArchive(false);
    if (!header.contains(e.target)) setMenu(false);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { setArchive(false); setMenu(false); }
  });
  header.querySelectorAll(".archive-panel a").forEach(function (a) {
    a.addEventListener("click", function () { setArchive(false); setMenu(false); });
  });
})();
