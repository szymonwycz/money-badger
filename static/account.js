/* ACCOUNT tab — your own password, and for an admin, who else has a login.
   The people section is only rendered into the page for an admin, so everything
   that touches it bails on the missing element instead of asking again. */

async function loadUsers() {
  const list = document.getElementById('user-list');
  if (!list) return;
  const users = await api('/api/users');
  list.innerHTML = '';
  for (const u of users) {
    const row = document.createElement('div');
    row.className = 'acct-row';
    row.innerHTML = `
      <span class="acct-name">${esc(u.username)}</span>
      <span class="acct-tag">${u.is_admin ? 'manages people' : ''}</span>
      <button class="btn btn-blue user-reset">Reset password</button>
      <button class="icon-btn user-del" title="Remove">×</button>
    `;
    row.querySelector('.user-reset').addEventListener('click', async () => {
      /* Nobody can read the old one, so a reset is the only way back in — the
         admin hands over what they typed here and the user changes it after. */
      const pw = prompt(`New password for ${u.username}:`);
      if (!pw) return;
      await api(`/api/users/${u.id}/password`, {
        method: 'PUT', body: JSON.stringify({ password: pw }) });
      toast(`Password reset for ${u.username}`);
    });
    row.querySelector('.user-del').addEventListener('click', async () => {
      if (!confirm(`Remove ${u.username}? Their transactions stay, without an author.`)) return;
      await api(`/api/users/${u.id}`, { method: 'DELETE' });
      loadUsers();
    });
    list.appendChild(row);
  }
}

async function addUser() {
  const username = document.getElementById('new-username').value;
  const password = document.getElementById('new-password').value;
  await api('/api/users', {
    method: 'POST',
    body: JSON.stringify({
      username, password,
      is_admin: document.getElementById('new-is-admin').checked,
    }),
  });
  document.getElementById('new-username').value = '';
  document.getElementById('new-password').value = '';
  document.getElementById('new-is-admin').checked = false;
  toast(`${username} can sign in now`);
  loadUsers();
}

async function changeOwnPassword() {
  const current = document.getElementById('pw-current');
  const fresh   = document.getElementById('pw-new');
  await api('/api/account/password', {
    method: 'PUT',
    body: JSON.stringify({ current_password: current.value, password: fresh.value }),
  });
  current.value = '';
  fresh.value = '';
  toast('Password changed');
}

document.getElementById('btn-change-pw').addEventListener('click', changeOwnPassword);
const addBtn = document.getElementById('btn-add-user');
if (addBtn) addBtn.addEventListener('click', addUser);
loadUsers();
