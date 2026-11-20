// 웹앱에서 사용자에게 보이는 고정 문구는 모두 이 파일에 둔다.
// 다른 파일은 이 객체(셸) 또는 app.strings(위젯)로만 문구를 꺼낸다.

const pad = (n) => String(n).padStart(2, "0");
// "YYYY-MM-DD" → "MM.DD"
const shortDate = (s) => `${s.slice(5, 7)}.${s.slice(8, 10)}`;
// "YYYY-MM-DD" → "YYYY.MM.DD"
const fullDate = (s) => s.replace(/-/g, ".");

export default {
  lang: "ko",
  locale: "ko",

  // 셸
  skipToContent: "본문으로 건너뛰기",
  navLabel: "탭 메뉴",
  readOnly: "보기 전용",
  readOnlyTitle: "이 화면은 조회만 할 수 있습니다",
  asOf: "기준일",
  formatAsOf: (s) => fullDate(s),
  theme: "테마",
  themeToLight: "라이트",
  themeToDark: "다크",
  themeButtonLabel: (next) => (next === "dark" ? "다크모드로 전환" : "라이트모드로 전환"),
  retry: "다시 시도",

  appLoading: "화면 구성을 불러오는 중…",
  appErrorTitle: "화면 구성을 불러오지 못했습니다",
  appErrorBody: "백엔드 서버가 실행 중인지 확인하세요. 터미널에서 make logs로 원인을 볼 수 있습니다.",
  noTabs: "표시할 탭이 없습니다. 설정 파일의 tabs를 확인하세요.",

  // 사용자 메뉴(레일 아래)
  account: "내 계정",
  accountMenuLabel: (email) => `내 계정: ${email}`,
  logout: "로그아웃",
  roleLabel: { admin: "어드민", operator: "운용진", user: "일반사용자" },
  sessionExpired: "로그인이 만료되었습니다. 다시 로그인하세요.",

  // 로그인 전 화면(login.html 등, auth.js)
  auth: {
    loginTitle: "로그인",
    loginIntro: "사내 이메일과 비밀번호로 로그인하세요.",
    email: "이메일",
    password: "비밀번호",
    loginSubmit: "로그인",
    loginSubmitting: "로그인 중…",
    loggedOut: "로그아웃했습니다.",
    required: "이메일과 비밀번호를 입력하세요.",
    errors: {
      invalid_credentials: "이메일 또는 비밀번호가 올바르지 않습니다.",
      email_not_verified: "메일 인증을 마쳐야 로그인할 수 있습니다. 받은 메일의 링크를 여세요.",
      account_suspended: "정지된 계정입니다. 운용자에게 문의하세요.",
      account_locked: "로그인에 여러 번 실패해 잠시 잠겼습니다. 15분 뒤에 다시 시도하거나 운용자에게 문의하세요.",
      rate_limited: "요청이 너무 많습니다. 잠시 뒤에 다시 시도하세요.",
      auth_unavailable: "로그인 서버에 문제가 있습니다. 관리자에게 알려 주세요(make logs).",
      network: "서버에 연결할 수 없습니다. 잠시 뒤에 다시 시도하세요.",
      server: (status) => `서버 오류가 발생했습니다(HTTP ${status}).`,
    },
  },

  // 섹션 공통
  sectionLoading: "불러오는 중…",
  dashboardLoading: "대시보드를 불러오는 중…",
  unsupportedSection: (type) => `지원하지 않는 섹션 유형입니다: ${type}`,

  // Superset 섹션 오류
  notAppliedTitle: "아직 반영되지 않았습니다",
  notAppliedBody: "설정이 Superset에 반영되기 전입니다. 터미널에서 make apply를 실행한 뒤 다시 시도하세요.",
  tokenErrorTitle: "접근 토큰을 받지 못했습니다",
  tokenErrorBody: {
    unknown_dashboard: "설정에 없는 대시보드입니다. 설정을 바꿨다면 make apply로 반영하세요.",
    server: (status) => `백엔드 서버가 응답하지 않았습니다(HTTP ${status}). make logs로 원인을 확인하세요.`,
    network: "백엔드 서버에 연결할 수 없습니다. 서버가 실행 중인지 확인하세요.",
  },
  connectErrorTitle: "Superset에 연결할 수 없습니다",
  connectErrorBody: (url) => `${url} 에 접속할 수 있는지 확인하세요. 브라우저에서 이 주소가 열려야 대시보드를 표시할 수 있습니다.`,

  // 데이터 섹션 오류(/api/data)
  dataErrorTitle: "데이터를 불러오지 못했습니다",
  dataErrorBody: {
    unknown_dataset: "위젯이 참조하는 데이터셋을 찾을 수 없습니다. 설정을 바꿨다면 make apply로 반영하세요.",
    not_loaded: "데이터가 아직 적재되지 않았습니다. make apply를 실행한 뒤 다시 시도하세요.",
    db_unavailable: "데이터베이스에 연결할 수 없습니다. make logs로 원인을 확인하세요.",
    server: (status) => `서버 오류가 발생했습니다(HTTP ${status}). make logs로 원인을 확인하세요.`,
    network: "백엔드 서버에 연결할 수 없습니다. 서버가 실행 중인지 확인하세요.",
  },
  widgetErrorTitle: "위젯을 그리지 못했습니다",
  widgetColumnMissing: (col) => `데이터에 '${col}' 열이 없습니다. 설정의 열 이름을 확인하세요.`,
  truncated: (n) => `행이 많아 앞 ${n.toLocaleString("ko")}행만 표시합니다.`,

  // 날짜
  shortDate,
  fullDate,
  dateRange: (a, b) => `${fullDate(a)} – ${fullDate(b)}`,
  shortRange: (a, b) => `${shortDate(a)}–${shortDate(b)}`,
  monthLabel: (m) => `${m}월`,
  yearMonthLabel: (y, m) => `${y}.${pad(m)}`,
  today: (asOf) => `오늘 ${shortDate(asOf)}`,
  todayLegend: "┆ 오늘",

  // 간트
  ganttEmpty: "표시할 막대가 없습니다.",
  progress: (p) => `${p}%`,

  // 칸반
  all: "전체",
  kanbanEmpty: "카드 없음",
  kanbanReadOnly: "보기 전용, 카드 이동 불가",
  cardCount: (n) => `${n}장`,
  columnCountLabel: (label, n) => `${label}, 카드 ${n}장`,
};
