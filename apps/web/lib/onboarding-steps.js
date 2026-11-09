import { OrganisationStep } from "@/components/organisation-step";
import { LanguageStep } from "@/components/onboarding/language-step";
import { AiStep } from "@/components/onboarding/ai-step";
import { PlanStep } from "@/components/onboarding/plan-step";
import { EmailStep } from "@/components/onboarding/email-step";
import { VoiceStep } from "@/components/onboarding/voice-step";

// The onboarding wizard renders whatever is in this array. Later phases append
// steps (plan & limit behaviour, phone, email).
// Each Component receives { onComplete(result), context } where context holds
// the merged results of earlier steps (e.g. orgId).
export const ONBOARDING_STEPS = [
  { id: "organisation", title: "Create your organisation", Component: OrganisationStep },
  { id: "language", title: "Choose your language", Component: LanguageStep },
  { id: "ai", title: "Connect your AI", Component: AiStep },
  { id: "plan", title: "Choose your plan", Component: PlanStep },
  { id: "email", title: "Connect your support mailbox", Component: EmailStep },
  { id: "voice", title: "Set up phone calls", Component: VoiceStep },
];
