// The header menu on narrow screens (folded behind one button; closed again by a
// tap elsewhere or Escape), and the "on this page" list following the reader.
(function () {
  var top = document.querySelector('header.top');
  var button = top && top.querySelector('.menu-toggle');
  if (button) {
    var set = function (open) {
      top.classList.toggle('open', open);
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    button.addEventListener('click', function () { set(!top.classList.contains('open')); });
    document.addEventListener('click', function (e) { if (!top.contains(e.target)) set(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') set(false); });
  }

  var links = Array.prototype.slice.call(document.querySelectorAll('.toc-page a[href^="#"]'));
  if (!links.length || !('IntersectionObserver' in window)) return;
  var byId = {};
  links.forEach(function (a) { byId[decodeURIComponent(a.getAttribute('href').slice(1))] = a; });
  var headings = Object.keys(byId).map(function (id) { return document.getElementById(id); }).filter(Boolean);
  var visible = {};
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) { visible[e.target.id] = e.isIntersecting; });
    var current = null;
    for (var i = 0; i < headings.length; i++) {
      if (headings[i].getBoundingClientRect().top < window.innerHeight * 0.35) current = headings[i].id;
    }
    links.forEach(function (a) { a.classList.remove('current'); });
    if (current && byId[current]) byId[current].classList.add('current');
  }, { rootMargin: '0px 0px -60% 0px' });
  headings.forEach(function (h) { observer.observe(h); });
})();
