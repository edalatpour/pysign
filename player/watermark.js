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
        width: "max-content",
        "max-width": "calc(100vw - 32px)",
        "box-sizing": "border-box",
        padding: "6px 8px",
        "border-radius": "8px",
        background: "rgba(20, 43, 50, 0.468)",
        opacity: "1",
        "pointer-events": "none",
        "user-select": "none",
        "z-index": "2147483647",
        display: "flex",
        "align-items": "center",
        gap: "8px",
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
    svg.style.setProperty("width", "100px");
    svg.style.setProperty("height", "auto");
    svg.style.setProperty("opacity", "0.65");
    svg.style.setProperty("flex-shrink", "0");
    shadow.append(svg);

    let qrSvg = null;
    globalThis.__pisignUpdateQr = function (url, matrix) {
        // Navigation can finish between reading the URL and delivering its QR.
        if (url !== location.href) {
            return false;
        }
        if (qrSvg) {
            qrSvg.remove();
            qrSvg = null;
        }
        if (!matrix) {
            delete host.dataset.qrUrl;
            return true;
        }
        const size = matrix.length + 8;
        let path = "";
        for (let y = 0; y < matrix.length; y++) {
            for (let x = 0; x < matrix.length; x++) {
                if (matrix[y][x]) {
                    path += `M${x + 4},${y + 4}h1v1h-1z`;
                }
            }
        }
        qrSvg = createSvg({
            tag: "svg", attributes: {
                viewBox: `0 0 ${size} ${size}`, "shape-rendering": "crispEdges",
            }, text: "", children: [{
                tag: "path", attributes: {d: path, fill: "#000"}, text: "", children: [],
            }],
        });
        // Four white modules on every side are the QR quiet zone.
        qrSvg.style.setProperty("background", "#fff");
        qrSvg.style.setProperty("display", "block");
        qrSvg.style.setProperty("width", `${Math.max(100, size * 3)}px`);
        qrSvg.style.setProperty("height", `${Math.max(100, size * 3)}px`);
        qrSvg.style.setProperty("flex-shrink", "0");
        shadow.prepend(qrSvg);
        host.dataset.qrUrl = url;
        return true;
    };

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
