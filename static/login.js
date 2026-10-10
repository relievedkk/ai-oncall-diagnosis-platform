(() => {
    // Remove the legacy browser-stored machine credential from earlier versions.
    sessionStorage.removeItem('aiOnCallApiKey');

    const form = document.getElementById('loginForm');
    const username = document.getElementById('username');
    const password = document.getElementById('password');
    const remember = document.getElementById('remember');
    const error = document.getElementById('loginError');
    const button = document.getElementById('loginButton');
    const toggle = document.getElementById('togglePassword');

    const showError = (message) => {
        error.textContent = message || '';
    };

    const setLoading = (loading) => {
        button.disabled = loading;
        button.querySelector('span').textContent = loading ? '正在验证...' : '登录工作台';
    };

    toggle.addEventListener('click', () => {
        const visible = password.type === 'text';
        password.type = visible ? 'password' : 'text';
        toggle.setAttribute('aria-label', visible ? '显示密码' : '隐藏密码');
    });

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        showError('');
        if (!username.value.trim() || !password.value) {
            showError('请输入账号和密码');
            return;
        }

        setLoading(true);
        try {
            const response = await fetch('/api/auth/login', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    username: username.value.trim(),
                    password: password.value,
                    remember: remember.checked
                })
            });
            if (response.ok) {
                window.location.replace('/');
                return;
            }
            if (response.status === 429) {
                showError('尝试次数过多，请稍后再试');
                return;
            }
            const payload = await response.json().catch(() => ({}));
            showError(payload.detail || '登录失败，请检查账号和密码');
        } catch (requestError) {
            showError('暂时无法连接认证服务，请确认后端已经启动');
        } finally {
            setLoading(false);
        }
    });

    fetch('/api/auth/me', { credentials: 'same-origin' })
        .then((response) => {
            if (response.ok) window.location.replace('/');
        })
        .catch(() => {});
})();
