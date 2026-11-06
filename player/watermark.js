function (logo) {
    if (window !== window.top || !["http:", "https:"].includes(location.protocol)) {
        return;
    }
    if (globalThis.__pisignWatermarkInstalled) {
        return;
    }
    globalThis.__pisignWatermarkInstalled = true;

    const host = document.createElement("div");
    host.id = "pisign-watermark";
    host.setAttribute("aria-hidden", "true");
    host.inert = true;
    const styles = {
        all: "initial",
        position: "fixed",
        right: "16px",
        bottom: "16px",
        width: "116px",
        "max-width": "calc(100vw - 32px)",
        "box-sizing": "border-box",
        padding: "6px 8px",
        "border-radius": "8px",
        background: "rgba(20, 43, 50, 0.72)",
        opacity: "0.65",
        "pointer-events": "none",
        "user-select": "none",
        "z-index": "2147483647",
        display: "block",
        animation: "none",
        transition: "none",
    };
    for (const [property, value] of Object.entries(styles)) {
        host.style.setProperty(property, value, "important");
    }

    // DOM construction avoids Trusted HTML sinks and external image requests.
    function createSvg(node) {
        const element = document.createElementNS("http://www.w3.org/2000/svg", node.tag);
        for (const [name, value] of Object.entries(node.attributes)) {
            element.setAttribute(name, value);
        }
        element.textContent = node.text;
        for (const child of node.children) {
            element.append(createSvg(child));
        }
        return element;
    }
    const shadow = host.attachShadow({mode: "closed"});
    const svg = createSvg(logo);
    svg.style.setProperty("display", "block");
    svg.style.setProperty("width", "100%");
    svg.style.setProperty("height", "auto");
    shadow.append(svg);

    const observer = new MutationObserver(mount);
    function mount() {
        observer.disconnect();
        const root = document.documentElement;
        const fullscreen = document.fullscreenElement;
        // Replaced elements cannot render DOM children over their content.
        const target = fullscreen && !["VIDEO", "AUDIO", "IFRAME", "IMG", "CANVAS", "EMBED", "OBJECT"]
            .includes(fullscreen.tagName) ? fullscreen : root;
        if (target && host.parentNode !== target) {
            target.append(host);
        }
        observer.observe(document, {childList: true});
        if (root) {
            observer.observe(root, {childList: true});
        }
        if (target && target !== root) {
            observer.observe(target, {childList: true});
        }
    }
    document.addEventListener("fullscreenchange", mount);
    mount();
}
