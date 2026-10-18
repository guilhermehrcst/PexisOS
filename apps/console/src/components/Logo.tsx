/**
 * PROVISIONAL mark. No official PexisOS logo asset exists in the repository yet, so this
 * is a neutral placeholder (an isometric cube) and must be replaced by the official
 * identity once one is approved. It is not a proposed brand.
 */
export function Logo() {
  return (
    <span className="logo" title="Provisional mark: no official PexisOS logo asset exists yet">
      <svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true">
        <defs>
          <linearGradient id="logo-a" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#3b97ff" />
            <stop offset="1" stopColor="#1268f0" />
          </linearGradient>
        </defs>
        <path d="M17 2.5 30 9.6v14.8L17 31.5 4 24.4V9.6Z" fill="url(#logo-a)" />
        <path d="M17 2.5 30 9.6 17 16.8 4 9.6Z" fill="#7cbcff" />
        <path d="M17 16.8V31.5L4 24.4V9.6Z" fill="#1a73f2" />
        <path d="M17 16.8 30 9.6v14.8L17 31.5Z" fill="#0b5bd6" />
        <path d="M11.5 13.6 17 16.8l5.5-3.2V20L17 23.2 11.5 20Z" fill="#fff" opacity="0.92" />
      </svg>
      <span className="logo__word">PexisOS</span>
    </span>
  );
}
