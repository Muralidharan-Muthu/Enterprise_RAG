"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth } from "@/lib/auth";
import { Loader2, Mail, Lock, Eye, EyeOff, UserPlus, AtSign } from "lucide-react";

export default function SignupPage() {
  const { signup } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    // Client-side validation
    if (username.length < 3) {
      setError("Username must be at least 3 characters");
      return;
    }
    if (password.length < 6) {
      setError("Password must be at least 6 characters");
      return;
    }
    if (password !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }

    setLoading(true);
    try {
      await signup(username, email, password);
      router.push("/upload");
    } catch (err: any) {
      setError(err.message || "Signup failed");
    } finally {
      setLoading(false);
    }
  };

  // Password strength indicator
  const getStrength = (pw: string) => {
    let score = 0;
    if (pw.length >= 6) score++;
    if (pw.length >= 10) score++;
    if (/[A-Z]/.test(pw)) score++;
    if (/[0-9]/.test(pw)) score++;
    if (/[^A-Za-z0-9]/.test(pw)) score++;
    return score;
  };
  const strength = getStrength(password);
  const strengthColors = ["bg-red-500", "bg-orange-500", "bg-yellow-500", "bg-lime-500", "bg-emerald-500"];
  const strengthLabels = ["Very Weak", "Weak", "Fair", "Strong", "Very Strong"];

  return (
    <div className="w-full">
      {/* Logo */}
      <div className="text-center mb-8">
        <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-600 shadow-lg shadow-indigo-500/25 mb-4">
          <UserPlus className="w-7 h-7 text-white" />
        </div>
        <h1 className="text-2xl font-bold text-white">Create account</h1>
        <p className="text-sm text-gray-400 mt-1">
          Get started with Enterprise RAG
        </p>
      </div>

      {/* Card */}
      <div className="bg-white/[0.05] backdrop-blur-xl border border-white/[0.08] rounded-2xl p-8 shadow-2xl">
        <form onSubmit={handleSubmit} className="space-y-5">
          {/* Username */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-2">
              Username
            </label>
            <div className="relative">
              <AtSign className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
              <input
                id="signup-username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="johndoe"
                required
                minLength={3}
                maxLength={50}
                className="w-full pl-10 pr-4 py-3 bg-white/[0.05] border border-white/[0.1] rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 transition-all"
              />
            </div>
          </div>

          {/* Email */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-2">
              Email
            </label>
            <div className="relative">
              <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
              <input
                id="signup-email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                required
                className="w-full pl-10 pr-4 py-3 bg-white/[0.05] border border-white/[0.1] rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 transition-all"
              />
            </div>
          </div>

          {/* Password */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-2">
              Password
            </label>
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
              <input
                id="signup-password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
                minLength={6}
                className="w-full pl-10 pr-12 py-3 bg-white/[0.05] border border-white/[0.1] rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 transition-all"
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 transition-colors"
              >
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
            {/* Strength bar */}
            {password.length > 0 && (
              <div className="mt-2">
                <div className="flex gap-1">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <div
                      key={i}
                      className={`h-1 flex-1 rounded-full transition-all duration-300 ${i < strength ? strengthColors[strength - 1] : "bg-white/[0.08]"
                        }`}
                    />
                  ))}
                </div>
                <p className="text-xs text-gray-500 mt-1">
                  {strength > 0 ? strengthLabels[strength - 1] : "Enter a password"}
                </p>
              </div>
            )}
          </div>

          {/* Confirm Password */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-2">
              Confirm Password
            </label>
            <div className="relative">
              <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" />
              <input
                id="signup-confirm-password"
                type={showPassword ? "text" : "password"}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="••••••••"
                required
                className={`w-full pl-10 pr-4 py-3 bg-white/[0.05] border rounded-xl text-white placeholder-gray-500 focus:outline-none focus:ring-2 transition-all ${confirmPassword && confirmPassword !== password
                    ? "border-red-500/50 focus:ring-red-500/50"
                    : "border-white/[0.1] focus:ring-indigo-500/50 focus:border-indigo-500/50"
                  }`}
              />
            </div>
          </div>

          {/* Error */}
          {error && (
            <div className="px-4 py-3 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* Submit */}
          <button
            id="signup-submit"
            type="submit"
            disabled={loading}
            className="w-full py-3 px-4 bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white font-semibold rounded-xl shadow-lg shadow-indigo-500/25 transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Creating account...
              </>
            ) : (
              "Create Account"
            )}
          </button>
        </form>

        {/* Divider */}
        <div className="mt-6 text-center">
          <p className="text-sm text-gray-400">
            Already have an account?{" "}
            <Link
              href="/login"
              className="text-indigo-400 hover:text-indigo-300 font-medium transition-colors"
            >
              Sign In
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
