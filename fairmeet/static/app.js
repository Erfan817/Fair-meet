/* Progressive enhancements only. No storage, analytics, or third-party requests. */
(function () {
  'use strict';

  // Keep the current collaborator context while navigating this trip. The URL
  // remains the source of truth; no member or trip data is persisted locally.
  var current = new URL(window.location.href);
  var member = current.searchParams.get('member');
  var tripMatch = current.pathname.match(/^\/trip\/([^/]+)/);
  if (member && tripMatch) {
    document.querySelectorAll('a[href]').forEach(function (link) {
      var target = new URL(link.getAttribute('href'), current.origin);
      if (target.origin === current.origin &&
          (target.pathname === '/trip/' + tripMatch[1] ||
           target.pathname.indexOf('/trip/' + tripMatch[1] + '/') === 0) &&
          !target.searchParams.get('member')) {
        target.searchParams.set('member', member);
        link.setAttribute('href', target.pathname + target.search + target.hash);
      }
    });
  }

  document.querySelectorAll('[data-copy-target]').forEach(function (button) {
    button.addEventListener('click', function () {
      var field = document.getElementById(button.getAttribute('data-copy-target'));
      var feedback = document.getElementById('copy-feedback');
      if (!field) return;
      function fallback() {
        field.focus();
        field.select();
        if (feedback) feedback.textContent = '链接已选中，请复制后发给同行者。';
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(field.value).then(function () {
          if (feedback) feedback.textContent = '邀请链接已复制，可以发给同行者了。';
        }).catch(fallback);
      } else {
        fallback();
      }
    });
  });

  var createForm = document.querySelector('.create-form');
  if (createForm) {
    var start = createForm.elements.start_date;
    var end = createForm.elements.end_date;
    function validateDates() {
      end.setCustomValidity(start.value && end.value && end.value < start.value ? '结束日期不能早于开始日期。' : '');
    }
    start.addEventListener('change', validateDates);
    end.addEventListener('change', validateDates);
  }
})();
