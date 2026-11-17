// Read the spec before Telegram's script looks at the fragment (it appends its own launch
// parameters there). The spec never reaches the web host: browsers don't send the fragment.
window.FRIDAY_HASH = location.hash;
