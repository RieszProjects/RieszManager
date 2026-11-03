import { useMediaQuery } from "@mui/material";
import { layout } from "@/theme/tokens";

export const useIsMobile = () => useMediaQuery(`(max-width:${layout.mobileBreakpoint - 1}px)`);
export const useIsWide = () => useMediaQuery(`(min-width:${layout.wideBreakpoint}px)`);
