"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth";
import {
  Zap,
  Database,
  Layers,
  Network,
  Shield,
  FileSpreadsheet,
  FileText,
  Scale,
  Sparkles,
  ArrowRight,
  CheckCircle2,
  Cpu,
  Search,
  ChevronRight,
  GitBranch,
  Bot,
} from "lucide-react";

export default function LandingPage() {
  const { user } = useAuth();

  return (
    <div className="min-h-screen bg-[#18181b] text-white selection:bg-indigo-500/30 selection:text-indigo-200 overflow-x-hidden">
      {/* ── Background Glow Elements ─────────────────────────────────────── */}
      <div className="fixed inset-0 pointer-events-none z-0 overflow-hidden">
        <div className="absolute -top-48 left-1/2 -translate-x-1/2 w-[1000px] h-[500px] bg-gradient-to-b from-indigo-600/20 via-violet-600/15 to-transparent rounded-full blur-[140px] animate-pulse-slow" />
        <div className="absolute top-[30%] -left-64 w-[600px] h-[600px] bg-cyan-500/10 rounded-full blur-[160px] animate-pulse-slow [animation-delay:2s]" />
        <div className="absolute top-[60%] -right-64 w-[600px] h-[600px] bg-violet-600/15 rounded-full blur-[160px] animate-pulse-slow [animation-delay:4s]" />
        <div
          className="absolute inset-0 opacity-[0.02]"
          style={{
            backgroundImage: `linear-gradient(rgba(255,255,255,0.15) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.15) 1px, transparent 1px)`,
            backgroundSize: "48px 48px",
          }}
        />
      </div>

      {/* ── Top Navigation Bar ───────────────────────────────────────────── */}
      <header className="sticky top-0 z-50 border-b border-white/[0.08] bg-[#18181b]/85 backdrop-blur-xl">
        <div className="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
          <Link href="/" className="flex items-center gap-3 group">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center shadow-lg shadow-indigo-500/25 group-hover:scale-105 transition-transform">
              <Database className="w-5 h-5 text-white" />
            </div>
            <div>
              <span className="font-bold text-lg tracking-tight bg-gradient-to-r from-white via-gray-100 to-gray-400 bg-clip-text text-transparent">
                Multi-Store RAG
              </span>
              <span className="hidden sm:inline-block ml-2 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 rounded-full">
                Enterprise v2.0
              </span>
            </div>
          </Link>

          <nav className="hidden md:flex items-center gap-8 text-sm text-gray-400">
            <a href="#stores" className="hover:text-white transition-colors">
              Multi-Store Architecture
            </a>
            <a href="#features" className="hover:text-white transition-colors">
              Features
            </a>
            <a href="#workflow" className="hover:text-white transition-colors">
              How it Works
            </a>
          </nav>

          <div className="flex items-center gap-3">
            {user ? (
              <Link
                href="/upload"
                className="px-4 py-2 text-sm font-medium rounded-xl bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white shadow-lg shadow-indigo-500/25 transition-all flex items-center gap-2"
              >
                Go to Workspace
                <ArrowRight className="w-4 h-4" />
              </Link>
            ) : (
              <>
                <Link
                  href="/login"
                  className="px-4 py-2 text-sm font-medium text-gray-300 hover:text-white hover:bg-white/[0.06] rounded-xl transition-all"
                >
                  Sign In
                </Link>
                <Link
                  href="/signup"
                  className="px-4 py-2 text-sm font-medium rounded-xl bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white shadow-lg shadow-indigo-500/25 transition-all flex items-center gap-2"
                >
                  Get Started
                  <ChevronRight className="w-4 h-4" />
                </Link>
              </>
            )}
          </div>
        </div>
      </header>

      {/* ── Hero Section ─────────────────────────────────────────────────── */}
      <section className="relative z-10 pt-20 pb-24 px-6 max-w-7xl mx-auto text-center">
        {/* Release badge */}
        <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-white/[0.05] border border-white/[0.1] text-xs font-medium text-indigo-300 mb-8 animate-fade-in">
          <Sparkles className="w-3.5 h-3.5 text-indigo-400" />
          <span>Next-Generation Agentic Document Intelligence</span>
          <span className="w-1 h-1 rounded-full bg-indigo-400" />
          <span className="text-gray-400">Groq Multi-Model + Neo4j GraphRAG</span>
        </div>

        {/* Main Headline */}
        <h1 className="text-4xl sm:text-6xl lg:text-7xl font-extrabold tracking-tight max-w-5xl mx-auto leading-[1.1] mb-6">
          One Query Engine. <br />
          <span className="gradient-text">Five Specialized Stores.</span>
        </h1>

        {/* Subtitle */}
        <p className="text-lg sm:text-xl text-gray-400 max-w-3xl mx-auto leading-relaxed mb-10">
          Stop flattening complex documents into generic text embeddings. Multi-Store RAG automatically
          partitions PDFs, financials, contracts, and research into purpose-built Postgres stores and
          Neo4j knowledge graphs with sub-second accuracy.
        </p>

        {/* Action Buttons */}
        <div className="flex flex-col sm:flex-row items-center justify-center gap-4 max-w-md mx-auto mb-16">
          <Link
            href={user ? "/upload" : "/signup"}
            className="w-full sm:w-auto px-8 py-3.5 text-base font-semibold rounded-xl bg-gradient-to-r from-indigo-600 via-indigo-500 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white shadow-xl shadow-indigo-500/30 hover:shadow-indigo-500/50 hover:scale-[1.02] transition-all flex items-center justify-center gap-2.5"
          >
            <Zap className="w-5 h-5" />
            Launch AI Chatbot
          </Link>
          <Link
            href={user ? "/query" : "/login"}
            className="w-full sm:w-auto px-8 py-3.5 text-base font-medium rounded-xl bg-white/[0.05] hover:bg-white/[0.09] border border-white/[0.1] text-gray-200 hover:text-white transition-all flex items-center justify-center gap-2"
          >
            <Search className="w-4 h-4 text-indigo-400" />
            Explore Queries
          </Link>
        </div>

        {/* Mini stats row */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 max-w-4xl mx-auto pt-8 border-t border-white/[0.06]">
          <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.05]">
            <div className="text-2xl sm:text-3xl font-bold text-white font-mono">5 Stores</div>
            <div className="text-xs text-gray-400 mt-1">Multi-Modal Partitioning</div>
          </div>
          <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.05]">
            <div className="text-2xl sm:text-3xl font-bold text-cyan-400 font-mono">&lt; 300ms</div>
            <div className="text-xs text-gray-400 mt-1">Hybrid Retrieval Speed</div>
          </div>
          <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.05]">
            <div className="text-2xl sm:text-3xl font-bold text-indigo-400 font-mono">100%</div>
            <div className="text-xs text-gray-400 mt-1">Grounded Page Citations</div>
          </div>
          <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.05]">
            <div className="text-2xl sm:text-3xl font-bold text-violet-400 font-mono">120B</div>
            <div className="text-xs text-gray-400 mt-1">Groq Synthesis Reasoning</div>
          </div>
        </div>
      </section>

      {/* ── The 5 Specialized Stores Showcase ─────────────────────────────── */}
      <section id="stores" className="relative z-10 py-20 px-6 max-w-7xl mx-auto">
        <div className="text-center mb-16">
          <h2 className="text-xs font-bold uppercase tracking-widest text-indigo-400 mb-3">
            Architecture Breakdown
          </h2>
          <h3 className="text-3xl sm:text-4xl font-bold tracking-tight">
            Specialized Storage for Every Document Dimension
          </h3>
          <p className="text-gray-400 max-w-2xl mx-auto mt-3 text-sm sm:text-base">
            No single vector index fits all data. Documents are decomposed into specialized
            datastores optimized for precision queries.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {/* Store 1: Table Store */}
          <div className="group relative p-7 rounded-2xl bg-white/[0.03] hover:bg-white/[0.06] border border-white/[0.08] hover:border-indigo-500/40 transition-all duration-300">
            <div className="w-12 h-12 rounded-xl bg-emerald-500/15 border border-emerald-500/30 flex items-center justify-center mb-5 text-emerald-400 group-hover:scale-110 transition-transform">
              <FileSpreadsheet className="w-6 h-6" />
            </div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-lg font-semibold text-white">Financial Table Store</h4>
              <span className="text-[10px] font-mono uppercase bg-emerald-500/20 text-emerald-300 px-2 py-0.5 rounded">
                table_store
              </span>
            </div>
            <p className="text-sm text-gray-400 leading-relaxed mb-4">
              Row-window chunking + per-cell typed values (`table_cell_store`). Supports server-side
              SQL pushdown for mathematical calculations (`SUM`, `AVG`, `WHERE &gt; 50K`).
            </p>
            <div className="text-xs font-mono text-emerald-400/80 flex items-center gap-2">
              <CheckCircle2 className="w-3.5 h-3.5" />
              SQL Pushdown &amp; Markdown Grids
            </div>
          </div>

          {/* Store 2: Clause Store */}
          <div className="group relative p-7 rounded-2xl bg-white/[0.03] hover:bg-white/[0.06] border border-white/[0.08] hover:border-indigo-500/40 transition-all duration-300">
            <div className="w-12 h-12 rounded-xl bg-amber-500/15 border border-amber-500/30 flex items-center justify-center mb-5 text-amber-400 group-hover:scale-110 transition-transform">
              <Scale className="w-6 h-6" />
            </div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-lg font-semibold text-white">Legal Clause Store</h4>
              <span className="text-[10px] font-mono uppercase bg-amber-500/20 text-amber-300 px-2 py-0.5 rounded">
                clause_store
              </span>
            </div>
            <p className="text-sm text-gray-400 leading-relaxed mb-4">
              Extracts contractual clauses, risk levels (LOW/MEDIUM/HIGH/CRITICAL), governing law,
              contracting parties, and termination rights into queryable JSONB metadata.
            </p>
            <div className="text-xs font-mono text-amber-400/80 flex items-center gap-2">
              <CheckCircle2 className="w-3.5 h-3.5" />
              Risk Analysis &amp; Party Extraction
            </div>
          </div>

          {/* Store 3: GraphRAG Knowledge Store */}
          <div className="group relative p-7 rounded-2xl bg-white/[0.03] hover:bg-white/[0.06] border border-white/[0.08] hover:border-indigo-500/40 transition-all duration-300">
            <div className="w-12 h-12 rounded-xl bg-violet-500/15 border border-violet-500/30 flex items-center justify-center mb-5 text-violet-400 group-hover:scale-110 transition-transform">
              <Network className="w-6 h-6" />
            </div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-lg font-semibold text-white">Neo4j Graph Store</h4>
              <span className="text-[10px] font-mono uppercase bg-violet-500/20 text-violet-300 px-2 py-0.5 rounded">
                Neo4j GraphRAG
              </span>
            </div>
            <p className="text-sm text-gray-400 leading-relaxed mb-4">
              Extracts multi-document entities and relationships. Executes local multi-hop Cypher
              traversals and global Louvain community cluster summaries.
            </p>
            <div className="text-xs font-mono text-violet-400/80 flex items-center gap-2">
              <CheckCircle2 className="w-3.5 h-3.5" />
              Multi-Hop Cross-Document Graph
            </div>
          </div>

          {/* Store 4: Policy & Narrative Vector Store */}
          <div className="group relative p-7 rounded-2xl bg-white/[0.03] hover:bg-white/[0.06] border border-white/[0.08] hover:border-indigo-500/40 transition-all duration-300">
            <div className="w-12 h-12 rounded-xl bg-blue-500/15 border border-blue-500/30 flex items-center justify-center mb-5 text-blue-400 group-hover:scale-110 transition-transform">
              <FileText className="w-6 h-6" />
            </div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-lg font-semibold text-white">Semantic Vector Store</h4>
              <span className="text-[10px] font-mono uppercase bg-blue-500/20 text-blue-300 px-2 py-0.5 rounded">
                vector_store
              </span>
            </div>
            <p className="text-sm text-gray-400 leading-relaxed mb-4">
              High-density text chunks embedded with 1024-dimensional BAAI/bge-large-en-v1.5 vectors,
              indexed via HNSW cosine distance with Reciprocal Rank Fusion.
            </p>
            <div className="text-xs font-mono text-blue-400/80 flex items-center gap-2">
              <CheckCircle2 className="w-3.5 h-3.5" />
              Dense Embeddings + HNSW Index
            </div>
          </div>

          {/* Store 5: Research Document Store */}
          <div className="group relative p-7 rounded-2xl bg-white/[0.03] hover:bg-white/[0.06] border border-white/[0.08] hover:border-indigo-500/40 transition-all duration-300">
            <div className="w-12 h-12 rounded-xl bg-cyan-500/15 border border-cyan-500/30 flex items-center justify-center mb-5 text-cyan-400 group-hover:scale-110 transition-transform">
              <Layers className="w-6 h-6" />
            </div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-lg font-semibold text-white">Document Registry</h4>
              <span className="text-[10px] font-mono uppercase bg-cyan-500/20 text-cyan-300 px-2 py-0.5 rounded">
                document_store
              </span>
            </div>
            <p className="text-sm text-gray-400 leading-relaxed mb-4">
              Retains page hierarchies, section headers, bibliographies, and bounding boxes for
              interactive PDF preview and high-fidelity source verification.
            </p>
            <div className="text-xs font-mono text-cyan-400/80 flex items-center gap-2">
              <CheckCircle2 className="w-3.5 h-3.5" />
              Source Verification &amp; Page BBoxes
            </div>
          </div>

          {/* Store 6: Image & Visual Store */}
          <div className="group relative p-7 rounded-2xl bg-white/[0.03] hover:bg-white/[0.06] border border-white/[0.08] hover:border-indigo-500/40 transition-all duration-300">
            <div className="w-12 h-12 rounded-xl bg-pink-500/15 border border-pink-500/30 flex items-center justify-center mb-5 text-pink-400 group-hover:scale-110 transition-transform">
              <Sparkles className="w-6 h-6" />
            </div>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-lg font-semibold text-white">Visual &amp; Figure Store</h4>
              <span className="text-[10px] font-mono uppercase bg-pink-500/20 text-pink-300 px-2 py-0.5 rounded">
                image_store
              </span>
            </div>
            <p className="text-sm text-gray-400 leading-relaxed mb-4">
              Crops charts, diagrams, and visual tables directly from pages. Synthesizes visual
              descriptions via VLM and indexes OCR text for visual search.
            </p>
            <div className="text-xs font-mono text-pink-400/80 flex items-center gap-2">
              <CheckCircle2 className="w-3.5 h-3.5" />
              Visual Table Crops &amp; VLM OCR
            </div>
          </div>
        </div>
      </section>

      {/* ── Key Features ──────────────────────────────────────────────────── */}
      <section id="features" className="relative z-10 py-20 px-6 max-w-7xl mx-auto border-t border-white/[0.06]">
        <div className="text-center mb-16">
          <h2 className="text-xs font-bold uppercase tracking-widest text-indigo-400 mb-3">
            Core Superpowers
          </h2>
          <h3 className="text-3xl sm:text-4xl font-bold tracking-tight">
            Built for Enterprise-Scale Precision
          </h3>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
          <div className="p-8 rounded-2xl bg-white/[0.02] border border-white/[0.06] hover:bg-white/[0.04] transition-all">
            <div className="w-10 h-10 rounded-lg bg-indigo-500/20 text-indigo-400 flex items-center justify-center mb-5">
              <Bot className="w-5 h-5" />
            </div>
            <h4 className="text-lg font-semibold text-white mb-2">Groq Multi-Model Mesh</h4>
            <p className="text-sm text-gray-400 leading-relaxed">
              Uses purpose-specific models: fast 20B for routing and entity extraction, and deep 120B
              for complex synthesis and multi-hop reasoning.
            </p>
          </div>

          <div className="p-8 rounded-2xl bg-white/[0.02] border border-white/[0.06] hover:bg-white/[0.04] transition-all">
            <div className="w-10 h-10 rounded-lg bg-cyan-500/20 text-cyan-400 flex items-center justify-center mb-5">
              <Search className="w-5 h-5" />
            </div>
            <h4 className="text-lg font-semibold text-white mb-2">Hybrid Fusion &amp; Reranking</h4>
            <p className="text-sm text-gray-400 leading-relaxed">
              Combines BM25 keyword search, dense BGE vectors, and cross-encoder reranking
              (`ms-marco-MiniLM-L-6-v2`) to eliminate hallucinations.
            </p>
          </div>

          <div className="p-8 rounded-2xl bg-white/[0.02] border border-white/[0.06] hover:bg-white/[0.04] transition-all">
            <div className="w-10 h-10 rounded-lg bg-violet-500/20 text-violet-400 flex items-center justify-center mb-5">
              <Shield className="w-5 h-5" />
            </div>
            <h4 className="text-lg font-semibold text-white mb-2">Supabase Secure Auth</h4>
            <p className="text-sm text-gray-400 leading-relaxed">
              Full user authentication with bcrypt hashed credentials, stateless JWT tokens, and
              isolated document workspaces.
            </p>
          </div>
        </div>
      </section>

      {/* ── Workflow / How it works ────────────────────────────────────────── */}
      <section id="workflow" className="relative z-10 py-20 px-6 max-w-7xl mx-auto border-t border-white/[0.06]">
        <div className="text-center mb-16">
          <h2 className="text-xs font-bold uppercase tracking-widest text-indigo-400 mb-3">
            Pipeline Lifecycle
          </h2>
          <h3 className="text-3xl sm:text-4xl font-bold tracking-tight">
            How Documents Turn into Intelligence
          </h3>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-4 gap-6 relative">
          <div className="p-6 rounded-2xl bg-white/[0.02] border border-white/[0.06] relative">
            <div className="text-3xl font-mono font-bold text-indigo-400/40 mb-3">01</div>
            <h4 className="text-base font-semibold text-white mb-2">Upload &amp; OCR Parse</h4>
            <p className="text-xs text-gray-400 leading-relaxed">
              Files are ingested via Docling / PyMuPDF, extracting raw text, visual crops, and table grids.
            </p>
          </div>

          <div className="p-6 rounded-2xl bg-white/[0.02] border border-white/[0.06] relative">
            <div className="text-3xl font-mono font-bold text-indigo-400/40 mb-3">02</div>
            <h4 className="text-base font-semibold text-white mb-2">Multi-Store Partition</h4>
            <p className="text-xs text-gray-400 leading-relaxed">
              Content is routed to tables, clauses, vector chunks, and Neo4j entity nodes simultaneously.
            </p>
          </div>

          <div className="p-6 rounded-2xl bg-white/[0.02] border border-white/[0.06] relative">
            <div className="text-3xl font-mono font-bold text-indigo-400/40 mb-3">03</div>
            <h4 className="text-base font-semibold text-white mb-2">Query Planning &amp; Rerank</h4>
            <p className="text-xs text-gray-400 leading-relaxed">
              Intent classifier routes queries to semantic, SQL, or graph paths, then fuses with cross-encoder.
            </p>
          </div>

          <div className="p-6 rounded-2xl bg-white/[0.02] border border-white/[0.06] relative">
            <div className="text-3xl font-mono font-bold text-indigo-400/40 mb-3">04</div>
            <h4 className="text-base font-semibold text-white mb-2">Cited LLM Synthesis</h4>
            <p className="text-xs text-gray-400 leading-relaxed">
              Generates stream answers with exact page numbers, table badges, and confidence metrics.
            </p>
          </div>
        </div>
      </section>

      {/* ── Call to Action Banner ────────────────────────────────────────── */}
      <section className="relative z-10 py-20 px-6 max-w-5xl mx-auto text-center">
        <div className="p-10 sm:p-14 rounded-3xl bg-gradient-to-b from-indigo-900/40 via-violet-900/20 to-transparent border border-indigo-500/30 shadow-2xl relative overflow-hidden">
          <div className="absolute top-0 right-0 w-80 h-80 bg-indigo-500/20 rounded-full blur-[100px] pointer-events-none" />
          <h3 className="text-3xl sm:text-4xl font-bold text-white mb-4">
            Ready to explore your enterprise documents?
          </h3>
          <p className="text-gray-300 max-w-xl mx-auto mb-8 text-sm sm:text-base">
            Create an account in seconds or sign in to start uploading PDFs, financial reports, and legal agreements.
          </p>
          <div className="flex flex-col sm:flex-row items-center justify-center gap-4">
            <Link
              href={user ? "/upload" : "/signup"}
              className="px-8 py-3.5 rounded-xl bg-white text-gray-950 font-semibold hover:bg-gray-100 shadow-xl hover:scale-105 transition-all flex items-center gap-2"
            >
              Get Started Now
              <ArrowRight className="w-4 h-4 text-gray-950" />
            </Link>
          </div>
        </div>
      </section>

      {/* ── Footer ────────────────────────────────────────────────────────── */}
      <footer className="relative z-10 border-t border-white/[0.06] py-10 px-6 max-w-7xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-4 text-xs text-gray-500">
        <div className="flex items-center gap-2">
          <Database className="w-4 h-4 text-indigo-400" />
          <span className="font-semibold text-gray-400">Multi-Store RAG System</span>
          <span>© 2026 Enterprise Intelligence</span>
        </div>
        <div className="flex items-center gap-6">
          <span className="flex items-center gap-1.5 text-emerald-400">
            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
            All Systems Operational
          </span>
        </div>
      </footer>
    </div>
  );
}
