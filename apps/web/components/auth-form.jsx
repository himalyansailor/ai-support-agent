"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

// Only same-site relative paths are honoured, to avoid open redirects.
export function safeNext(next) {
  if (typeof next !== "string") return null;
  if (!next.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
    return null;
  }
  return next;
}

function requestedNext() {
  if (typeof window === "undefined") return null;
  return safeNext(new URLSearchParams(window.location.search).get("next"));
}

export function destinationFor(memberships, next = null) {
  if (next) return next;
  return memberships.length === 0
    ? "/onboarding"
    : `/orgs/${memberships[0].organisation_id}`;
}

// Shared form for /login and /signup. mode: "login" | "signup".
export function AuthForm({ mode }) {
  const isSignup = mode === "signup";
  const router = useRouter();
  const { user, memberships, loading, login, signup } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  // Already signed in (e.g. session restored on reload): skip the form.
  useEffect(() => {
    if (!loading && user) router.replace(destinationFor(memberships, requestedNext()));
  }, [loading, user, memberships, router]);

  async function onSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const ms = isSignup
        ? await signup(email, password, name)
        : await login(email, password);
      router.replace(destinationFor(ms, requestedNext()));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Something went wrong");
      setSubmitting(false);
    }
  }

  return (
    <main className="flex flex-1 items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>{isSignup ? "Create your account" : "Log in"}</CardTitle>
          <CardDescription>
            {isSignup
              ? "Start with your email and a password."
              : "Welcome back."}
          </CardDescription>
        </CardHeader>
        <form onSubmit={onSubmit}>
          <CardContent className="flex flex-col gap-4">
            {isSignup && (
              <div className="flex flex-col gap-2">
                <Label htmlFor="name">Name</Label>
                <Input
                  id="name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  autoComplete="name"
                  required
                />
              </div>
            )}
            <div className="flex flex-col gap-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={isSignup ? "new-password" : "current-password"}
                minLength={isSignup ? 10 : undefined}
                required
              />
              {isSignup && (
                <p className="text-xs text-muted-foreground">
                  At least 10 characters.
                </p>
              )}
            </div>
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
          </CardContent>
          <CardFooter className="mt-4 flex flex-col gap-3">
            <Button type="submit" className="w-full" disabled={submitting || loading}>
              {submitting ? "Please wait..." : isSignup ? "Sign up" : "Log in"}
            </Button>
            <p className="text-sm text-muted-foreground">
              {isSignup ? "Already have an account? " : "New here? "}
              <Link
                href={isSignup ? "/login" : "/signup"}
                className="underline"
              >
                {isSignup ? "Log in" : "Sign up"}
              </Link>
            </p>
          </CardFooter>
        </form>
      </Card>
    </main>
  );
}
