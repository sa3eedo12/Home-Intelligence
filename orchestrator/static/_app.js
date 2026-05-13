(() => {
  function ensureToastStack() {
    let stack = document.getElementById('toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.id = 'toast-stack';
      stack.className = 'toast-stack';
      stack.setAttribute('aria-live', 'polite');
      document.body.appendChild(stack);
    }
    return stack;
  }

  function asErrorMessage(error) {
    if (!error) return 'Something went wrong.';
    return error.message || String(error);
  }

  const Toast = {
    show(message, kind = 'info', timeout = 4000) {
      const stack = ensureToastStack();
      const toast = document.createElement('div');
      const safeKind = ['success', 'warning', 'danger', 'info'].includes(kind) ? kind : 'info';
      toast.className = `toast toast-${safeKind}`;
      toast.setAttribute('role', safeKind === 'danger' ? 'alert' : 'status');
      toast.innerHTML = '<div class="toast-message"></div><button class="toast-close" type="button" aria-label="Dismiss notification">×</button>';
      toast.querySelector('.toast-message').textContent = message;
      const remove = () => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(6px)';
        setTimeout(() => toast.remove(), 160);
      };
      toast.querySelector('.toast-close').addEventListener('click', remove);
      stack.appendChild(toast);
      if (timeout > 0) setTimeout(remove, timeout);
      return toast;
    },
  };

  const Theme = {
    apply(name) {
      const requested = name || localStorage.getItem('theme') || 'dark';
      const theme = requested === 'light' ? 'light' : 'dark';
      document.documentElement.dataset.theme = theme;
      localStorage.setItem('theme', theme);
      document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
        button.setAttribute('aria-pressed', String(theme === 'light'));
        button.textContent = theme === 'light' ? '☀️' : '🌙';
        button.title = `Switch to ${theme === 'light' ? 'dark' : 'light'} theme`;
      });
      return theme;
    },
    toggle() {
      const current = document.documentElement.dataset.theme || localStorage.getItem('theme') || 'dark';
      return Theme.apply(current === 'light' ? 'dark' : 'light');
    },
  };

  const Modal = {
    open(id) {
      const dialog = typeof id === 'string' ? document.getElementById(id) : id;
      if (!dialog) return;
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', 'open');
    },
    close(id) {
      const dialog = typeof id === 'string' ? document.getElementById(id) : id;
      if (!dialog) return;
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
    },
  };

  function formatTimeAgo(date) {
    const value = date instanceof Date ? date : new Date(date);
    if (Number.isNaN(value.getTime())) return '';
    const seconds = Math.max(0, Math.floor((Date.now() - value.getTime()) / 1000));
    if (seconds < 45) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  }

  function debounce(fn, ms) {
    let timer = null;
    return function debounced(...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  function throttle(fn, ms) {
    let last = 0;
    let timer = null;
    return function throttled(...args) {
      const now = Date.now();
      const remaining = ms - (now - last);
      if (remaining <= 0) {
        clearTimeout(timer);
        timer = null;
        last = now;
        fn.apply(this, args);
      } else if (!timer) {
        timer = setTimeout(() => {
          last = Date.now();
          timer = null;
          fn.apply(this, args);
        }, remaining);
      }
    };
  }

  async function parseResponse(response) {
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) return response.json();
    const text = await response.text();
    return text ? { message: text } : {};
  }

  async function apiRequest(url, options = {}) {
    const { toastErrors = true, ...fetchOptions } = options;
    try {
      const response = await fetch(url, fetchOptions);
      const payload = await parseResponse(response).catch(() => ({}));
      if (!response.ok) {
        const detail = payload.detail || payload.message || `HTTP ${response.status}`;
        throw new Error(detail);
      }
      return payload;
    } catch (error) {
      if (toastErrors) Toast.show(asErrorMessage(error), 'danger');
      throw error;
    }
  }

  function apiGet(url, options = {}) {
    return apiRequest(url, { method: 'GET', cache: 'no-store', ...options });
  }

  function apiPost(url, body, options = {}) {
    const headers = { ...(options.headers || {}) };
    let payload;
    if (body !== undefined) {
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
      payload = typeof body === 'string' ? body : JSON.stringify(body);
    }
    return apiRequest(url, { method: 'POST', ...options, headers, body: payload });
  }

  window.Toast = Toast;
  window.Theme = Theme;
  window.Modal = Modal;
  window.formatTimeAgo = formatTimeAgo;
  window.debounce = debounce;
  window.throttle = throttle;
  window.apiPost = apiPost;
  window.apiGet = apiGet;

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-theme-toggle]');
    if (button) Theme.toggle();
  });

  document.addEventListener('DOMContentLoaded', () => Theme.apply(localStorage.getItem('theme') || document.documentElement.dataset.theme || 'dark'));
})();
