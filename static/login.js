const form = document.getElementById('form');
const errorBox = document.getElementById('error');
const submit = document.getElementById('submit');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  errorBox.classList.remove('show');
  submit.disabled = true;
  submit.textContent = 'Signing in…';

  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      body: new FormData(form),
    });
    if (response.ok) {
      window.location.href = '/';
      return;
    }
    const data = await response.json().catch(() => ({}));
    errorBox.textContent = data.detail || 'Sign-in failed.';
    errorBox.classList.add('show');
  } catch {
    errorBox.textContent = 'Could not reach the dashboard.';
    errorBox.classList.add('show');
  }
  submit.disabled = false;
  submit.textContent = 'Sign in';
});
