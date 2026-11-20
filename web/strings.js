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
  adminLink: "운용",
  noAccessTitle: "접근할 수 있는 대시보드가 없습니다",
  noAccessBody: "운용자에게 요청하세요.",
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
    toSignup: "가입하기",
    toForgot: "비밀번호를 잊으셨나요?",
    toLogin: "로그인 화면으로",
    mailTestBanner: "메일 테스트 모드: 메일이 실제로 발송되지 않습니다. 운용자가 운용 화면의 테스트 메일함에서 링크를 확인합니다.",
    resend: "인증 메일 다시 보내기",
    resent: "인증 메일을 다시 보냈습니다. 받은 메일의 링크를 여세요.",
    // 가입
    signupTitle: "가입하기",
    signupIntro: "가입하면 인증 메일을 보냅니다. 메일의 링크를 열어야 로그인할 수 있습니다.",
    emailHint: (domains) => `사내 이메일(${domains.map((d) => "@" + d).join(", ")})로 가입해야 합니다`,
    passwordConfirm: "비밀번호 확인",
    passwordHint: (min) => `${min}자 이상. 이메일의 @ 앞부분이나 흔한 비밀번호는 쓸 수 없습니다.`,
    mismatch: "두 비밀번호가 다릅니다.",
    signupSubmit: "가입하기",
    signupDone: "메일을 확인하세요. 인증 링크를 보냈습니다(24시간 유효). 메일이 오지 않으면 주소를 확인하고 다시 가입하세요.",
    // 비밀번호 찾기
    forgotTitle: "비밀번호 찾기",
    forgotIntro: "가입한 이메일을 입력하면 비밀번호를 다시 정하는 링크를 보냅니다.",
    forgotSubmit: "링크 받기",
    forgotDone: "가입된 주소라면 메일을 보냈습니다. 링크는 30분 동안 한 번만 쓸 수 있습니다.",
    // 새 비밀번호(재설정·초대 수락)
    resetTitle: "새 비밀번호 정하기",
    resetIntro: "앞으로 로그인할 때 쓸 비밀번호를 정하세요.",
    newPassword: "새 비밀번호",
    resetSubmit: "비밀번호 정하기",
    resetDone: "비밀번호를 정했습니다. 새 비밀번호로 로그인하세요.",
    noToken: "링크가 올바르지 않습니다. 메일의 링크를 그대로 여세요.",
    // 메일 인증
    verifyTitle: "메일 인증",
    verifying: "메일 인증 중…",
    verifyDone: "메일 인증을 마쳤습니다. 이제 로그인할 수 있습니다.",
    weak: {
      too_short: (min) => `비밀번호는 ${min}자 이상이어야 합니다.`,
      too_long: () => "비밀번호는 128자 이하여야 합니다.",
      same_as_email: () => "비밀번호가 이메일의 @ 앞부분과 같으면 안 됩니다.",
      too_common: () => "흔히 쓰이는 비밀번호입니다. 다른 비밀번호를 쓰세요.",
    },
    errors: {
      invalid_credentials: "이메일 또는 비밀번호가 올바르지 않습니다.",
      email_not_verified: "메일 인증을 마쳐야 로그인할 수 있습니다. 받은 메일의 링크를 여세요.",
      account_suspended: "정지된 계정입니다. 운용자에게 문의하세요.",
      account_locked: "로그인에 여러 번 실패해 잠시 잠겼습니다. 15분 뒤에 다시 시도하거나 운용자에게 문의하세요.",
      invalid_email: "이메일 형식이 올바르지 않습니다.",
      email_domain_not_allowed: "사내 이메일로만 가입할 수 있습니다.",
      weak_password: "비밀번호 정책에 맞지 않습니다.",
      token_expired: "링크가 만료되었거나 이미 쓰였습니다. 다시 요청하세요.",
      rate_limited: "요청이 너무 많습니다. 잠시 뒤에 다시 시도하세요.",
      auth_unavailable: "로그인 서버에 문제가 있습니다. 관리자에게 알려 주세요(make logs).",
      network: "서버에 연결할 수 없습니다. 잠시 뒤에 다시 시도하세요.",
      server: (status) => `서버 오류가 발생했습니다(HTTP ${status}).`,
    },
  },

  // 운용 화면(web/admin/, admin.js)
  admin: {
    title: "운용",
    back: "대시보드로",
    loading: "불러오는 중…",
    menu: { groups: "그룹", outbox: "테스트 메일함" },
    errors: {
      forbidden: "권한이 없습니다.",
      group_exists: "같은 이름의 그룹이 있습니다.",
      unknown_group: "없는 그룹입니다. 목록을 다시 불러오세요.",
      unknown_tab: "설정에 없는 탭이 들어 있습니다. 화면을 다시 불러오세요.",
      invalid_request: "입력값을 확인하세요.",
      auth_unavailable: "인증 서버에 문제가 있습니다(make logs).",
      network: "서버에 연결할 수 없습니다.",
      server: (status) => `서버 오류가 발생했습니다(HTTP ${status}).`,
    },
    saved: "저장했습니다.",
    save: "저장",
    cancel: "취소",
    remove: "빼기",
    add: "추가",
    del: "삭제",
    prev: "이전",
    next: "다음",
    pageInfo: (from, to, total) => `${total}개 중 ${from}–${to}`,
    yes: "예",
    no: "아니요",
    // 그룹
    groupsTitle: "그룹",
    groupsIntro: "일반사용자는 속한 그룹들이 허용한 탭만 봅니다. 운용자는 모든 탭을 봅니다.",
    newGroup: "그룹 만들기",
    groupName: "이름",
    groupDescription: "설명",
    groupDefault: "기본 그룹",
    groupDefaultHint: "새로 가입하거나 초대를 수락한 계정을 이 그룹에 자동으로 넣습니다.",
    memberCount: "구성원",
    tabCount: "탭",
    noGroups: "아직 그룹이 없습니다. 그룹을 만들고 허용할 탭과 구성원을 정하세요.",
    backToGroups: "그룹 목록",
    groupTabs: "허용 탭",
    groupTabsIntro: (project) => `현재 프로젝트(${project})의 탭 중 이 그룹이 볼 수 있는 탭을 고릅니다.`,
    staleTabs: "다른 프로젝트·없는 탭",
    staleTabsIntro: "판정에는 현재 프로젝트의 있는 탭만 쓰입니다. 필요 없으면 빼세요.",
    missingTab: "없는 탭",
    otherProject: (project) => `프로젝트 ${project}`,
    members: "구성원",
    noMembers: "구성원이 없습니다.",
    addMembers: "이메일로 추가",
    addMembersHint: "여러 개는 쉼표나 줄바꿈으로 구분합니다.",
    notFound: (list) => `찾지 못한 계정: ${list.join(", ")}`,
    deleteGroupConfirm: (name) => `그룹 '${name}'을 삭제할까요? 구성원과 허용 탭 연결만 지워지고 사용자 계정은 남습니다.`,
    status: { pending: "인증 전", invited: "초대됨", active: "활성", suspended: "정지" },
    // 테스트 메일함
    outboxTitle: "테스트 메일함",
    outboxIntro: "메일 테스트 모드에서 보낸 메일입니다(실제로 발송되지 않음, 7일 보관). 운용진에게는 일반사용자 계정 앞으로 간 메일만 보입니다.",
    outboxEmpty: "보낸 메일이 없습니다.",
    outboxTo: "받는 사람",
    outboxKind: "종류",
    outboxAt: "보낸 시각",
    outboxLink: "링크",
    openLink: "열기",
    mailKind: { verify_email: "가입 메일 인증", reset_password: "비밀번호 재설정", invite: "초대", already_registered: "이미 가입된 주소" },
    fmtDateTime: (iso) => { const d = new Date(iso); return isNaN(d) ? "—" : `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, "0")}.${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; },
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
