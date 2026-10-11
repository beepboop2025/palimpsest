// Copy examples without executing them or sending their contents anywhere.
const notice = document.createElement('p');
notice.className = 'example-copy-status'; notice.setAttribute('role', 'status');
notice.setAttribute('aria-live', 'polite'); document.body.append(notice);
let clearNotice;
for (const pre of document.querySelectorAll('pre')) {
  if (pre.closest('[hidden]') || pre.id || pre.querySelector('button')) continue;
  const code = pre.querySelector('code') || pre;
  if (!code.textContent.trim()) continue;
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'example-copy'; button.textContent = 'Copy example';
  button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(code.textContent.trim()); notice.textContent = 'Example copied. Review it before running.'; }
    catch { const range = document.createRange(); range.selectNodeContents(code); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); notice.textContent = 'Example selected. Use your device’s Copy command.'; }
    notice.classList.add('visible'); clearTimeout(clearNotice); clearNotice = setTimeout(() => notice.classList.remove('visible'), 7000);
  });
  pre.insertAdjacentElement('beforebegin', button);
}
