from fleet_manager.server import _rewrite_workspace_text


def test_vite_relative_asset_deps_stay_relative_for_host_proxy():
    content = 'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/home.js","assets/home.css"])))=>i.map(i=>d[i]);'

    rewritten = _rewrite_workspace_text(content, "axo-main", "", "text/javascript")

    assert '"assets/home.js"' in rewritten
    assert '"assets/home.css"' in rewritten
    assert '"/assets/home.css"' not in rewritten


def test_vite_relative_asset_deps_use_workspace_prefix_without_leading_slash():
    content = 'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/home.js","assets/home.css"])))=>i.map(i=>d[i]);'

    rewritten = _rewrite_workspace_text(content, "axo-main", "/workspace/axo-main", "text/javascript")

    assert '"workspace/axo-main/assets/home.js"' in rewritten
    assert '"workspace/axo-main/assets/home.css"' in rewritten
    assert '"/workspace/axo-main/assets/home.css"' not in rewritten


def test_absolute_asset_urls_keep_absolute_workspace_prefix():
    content = 'const css = "/assets/home.css";'

    rewritten = _rewrite_workspace_text(content, "axo-main", "/workspace/axo-main", "text/javascript")

    assert '"/workspace/axo-main/assets/home.css"' in rewritten
