import { alpha, createTheme } from "@mui/material/styles";
import { color, font, radius } from "./tokens";

export const theme = createTheme({
  palette: {
    mode: "dark",
    background: { default: color.bg, paper: color.bg2 },
    primary: { main: color.sage, light: color.sageStrong, dark: color.sageDeep, contrastText: "#10130f" },
    secondary: { main: color.amber, light: color.amberStrong, dark: color.amberDeep, contrastText: "#1a120b" },
    error: { main: color.danger },
    warning: { main: color.warn },
    info: { main: color.info },
    success: { main: color.sage },
    text: { primary: color.text, secondary: color.text2, disabled: color.text4 },
    divider: color.line,
    action: { hover: alpha(color.text, 0.05), selected: alpha(color.sage, 0.12) },
  },
  shape: { borderRadius: radius.md },
  typography: {
    fontFamily: font.sans,
    fontSize: 14,
    h1: { fontFamily: font.display, fontWeight: 600 },
    h2: { fontFamily: font.display, fontWeight: 600 },
    h3: { fontFamily: font.display, fontWeight: 600 },
    h4: { fontFamily: font.display, fontWeight: 600 },
    h5: { fontFamily: font.display, fontWeight: 600 },
    h6: { fontFamily: font.display, fontWeight: 600, fontSize: "1.2rem" },
    button: { textTransform: "none", fontWeight: 500, letterSpacing: 0.2 },
  },
  components: {
    MuiCssBaseline: { styleOverrides: { body: { backgroundColor: color.bg } } },
    MuiPaper: {
      styleOverrides: {
        root: { backgroundImage: "none", border: `1px solid ${color.line}` },
      },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: { borderRadius: radius.md, paddingInline: 14 },
        outlined: { borderColor: color.line, "&:hover": { borderColor: color.sageDeep, background: color.sageMuted } },
      },
    },
    MuiIconButton: {
      styleOverrides: { root: { borderRadius: radius.md, color: color.text2 } },
    },
    MuiTooltip: {
      defaultProps: { arrow: false, enterDelay: 400 },
      styleOverrides: {
        tooltip: {
          background: color.bg4,
          color: color.text,
          border: `1px solid ${color.line}`,
          fontSize: 12,
          fontWeight: 400,
          padding: "5px 9px",
        },
      },
    },
    MuiMenu: {
      styleOverrides: {
        paper: { background: color.bg2, border: `1px solid ${color.line}`, boxShadow: "0 18px 50px rgba(0,0,0,.45)" },
        list: { padding: 4 },
      },
    },
    MuiMenuItem: {
      styleOverrides: {
        root: { borderRadius: radius.sm, fontSize: 13.5, minHeight: 34, gap: 10 },
      },
    },
    MuiDialog: {
      styleOverrides: {
        paper: { background: color.bg1, border: `1px solid ${color.line}`, boxShadow: "0 30px 80px rgba(0,0,0,.55)" },
      },
    },
    MuiDrawer: {
      styleOverrides: { paper: { background: color.bg1, border: "none" } },
    },
    MuiTextField: { defaultProps: { variant: "outlined", size: "small" } },
    MuiOutlinedInput: {
      styleOverrides: {
        root: {
          background: color.bg,
          "& fieldset": { borderColor: color.line },
          "&:hover fieldset": { borderColor: color.text4 },
          "&.Mui-focused fieldset": { borderColor: color.sage, borderWidth: 1 },
        },
      },
    },
    MuiSnackbarContent: {
      styleOverrides: { root: { background: color.bg4, color: color.text, border: `1px solid ${color.line}` } },
    },
    MuiTab: { styleOverrides: { root: { textTransform: "none", minHeight: 40, fontSize: 13.5 } } },
    MuiTabs: { styleOverrides: { root: { minHeight: 40 }, indicator: { background: color.amber, height: 2 } } },
    MuiSwitch: {
      styleOverrides: {
        switchBase: { "&.Mui-checked + .MuiSwitch-track": { opacity: 0.6 } },
      },
    },
  },
});
