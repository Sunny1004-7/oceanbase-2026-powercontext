(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion || !("IntersectionObserver" in window)) {
    return;
  }

  var targets = document.querySelectorAll(
    ".pain-card, .flow-step, .feature-col, .shot-grid figure, .sample-card, .link-cards li, .timeline-demo, .tech-diagram"
  );

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }
    });
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });

  targets.forEach(function (el) {
    el.classList.add("reveal");
    observer.observe(el);
  });
})();