// AUTO-GENERATED from "Enterprise_Classification_Complete_With_Descriptions 1.xlsx".
// Do not edit by hand — regenerate from the source spreadsheet.
// Shape: Domain -> Sub_Domain -> Category -> SubCategory[] (each with a description).

export interface TaxonomyLeaf {
  name: string;
  description: string;
}

export type Taxonomy = Record<
  string,
  Record<string, Record<string, TaxonomyLeaf[]>>
>;

export const TAXONOMY: Taxonomy = {
  "Sales": {
    "Bookings": {
      "SOPs": [
        {
          "name": "Domestic_Orders",
          "description": "Standard operating procedures for domestic order booking and processing"
        },
        {
          "name": "International_Orders",
          "description": "Procedures for international order booking, customs, and cross-border logistics"
        }
      ],
      "Reports": [
        {
          "name": "Monthly_Booking_Report",
          "description": "Monthly analysis of booking trends, volumes, and revenue forecasting"
        },
        {
          "name": "Quarterly_Booking_Report",
          "description": "Quarterly business review of booking performance and strategic metrics"
        }
      ]
    },
    "Cancellation": {
      "Policies": [
        {
          "name": "Refund_Rules",
          "description": "Refund eligibility criteria, timeline, and approval process"
        }
      ],
      "SOPs": [
        {
          "name": "Cancellation_Process",
          "description": "Step-by-step procedures for processing customer order cancellations"
        }
      ]
    },
    "Order_Change": {
      "SOPs": [
        {
          "name": "Address_Change",
          "description": "Procedures for customer address modifications on active orders"
        },
        {
          "name": "Quantity_Change",
          "description": "Process for quantity adjustments on existing orders"
        }
      ]
    },
    "Shipment": {
      "Guidelines": [
        {
          "name": "Delivery_Timeline",
          "description": "Expected delivery timeframes and commitment levels for different regions"
        }
      ],
      "Reports": [
        {
          "name": "Delayed_Shipments",
          "description": "Tracking and analysis of shipment delays and root cause identification"
        }
      ]
    },
    "Pricing": {
      "Policies": [
        {
          "name": "Discount_Approval",
          "description": "Authorization levels and approval workflow for pricing discounts"
        }
      ]
    },
    "Contracts": {
      "Agreements": [
        {
          "name": "Customer_Agreements",
          "description": "Master service agreements and contract terms for key customers"
        }
      ]
    },
    "Leads": {
      "SOPs": [
        {
          "name": "Lead_Follow_Up",
          "description": "Lead nurturing and follow-up procedures to convert prospects into customers"
        }
      ],
      "Reports": [
        {
          "name": "Lead_Source_Analysis",
          "description": "Analysis of lead generation sources and conversion effectiveness by channel"
        }
      ]
    },
    "Territory_Management": {
      "SOPs": [
        {
          "name": "Territory_Assignment",
          "description": "Sales territory allocation and assignment procedures"
        }
      ],
      "Reports": [
        {
          "name": "Territory_Performance",
          "description": "Regional and territory-wise sales performance metrics and analysis"
        }
      ]
    },
    "Proposals": {
      "Templates": [
        {
          "name": "Quote_Template",
          "description": "Standard quotation and proposal templates for different product categories"
        }
      ],
      "SOPs": [
        {
          "name": "Proposal_Approval",
          "description": "Approval workflow and authority levels for sales proposals and quotes"
        }
      ]
    },
    "Performance": {
      "Reports": [
        {
          "name": "Sales_By_Channel",
          "description": "Sales performance breakdown by sales channel (direct, partner, online)"
        },
        {
          "name": "Employee_Commission",
          "description": "Commission calculation, incentive structures, and payment procedures"
        }
      ]
    },
    "Customer_Data": {
      "SOPs": [
        {
          "name": "Customer_Onboarding",
          "description": "New customer setup, account creation, and onboarding procedures"
        }
      ]
    },
    "Returns": {
      "Policies": [
        {
          "name": "Return_Policy",
          "description": "Return eligibility, timeframes, and condition requirements for products"
        }
      ],
      "SOPs": [
        {
          "name": "Return_Process",
          "description": "Step-by-step process for handling product returns and replacements"
        }
      ]
    }
  },
  "Marketing": {
    "Campaigns": {
      "Plans": [
        {
          "name": "Digital_Campaigns",
          "description": "Digital marketing campaign planning and execution roadmap"
        }
      ],
      "Reports": [
        {
          "name": "Campaign_Performance",
          "description": "Campaign metrics, ROI analysis, and performance reporting"
        }
      ]
    },
    "Leads": {
      "Reports": [
        {
          "name": "Lead_Conversion",
          "description": "Lead-to-customer conversion metrics and funnel analysis"
        }
      ],
      "SOPs": [
        {
          "name": "Lead_Qualification",
          "description": "Lead qualification criteria and scoring methodology"
        }
      ]
    },
    "Brand_Guidelines": {
      "Policies": [
        {
          "name": "Logo_Usage",
          "description": "Logo usage rights, dimensions, spacing, and color specifications"
        },
        {
          "name": "Brand_Tone",
          "description": "Brand voice, messaging guidelines, and communication standards"
        }
      ]
    },
    "Customer_Research": {
      "Reports": [
        {
          "name": "Market_Survey",
          "description": "Market research findings and customer survey analysis"
        }
      ]
    },
    "Events": {
      "Plans": [
        {
          "name": "Product_Launch",
          "description": "Product launch event planning and go-to-market strategy"
        }
      ]
    },
    "Content_Marketing": {
      "Plans": [
        {
          "name": "Content_Calendar",
          "description": "Editorial calendar and content publishing schedule"
        }
      ],
      "Manuals": [
        {
          "name": "Content_Guidelines",
          "description": "Writing standards, style guidelines, and content best practices"
        }
      ]
    },
    "Social_Media": {
      "Plans": [
        {
          "name": "Social_Strategy",
          "description": "Social media marketing strategy and channel management"
        }
      ],
      "Reports": [
        {
          "name": "Social_Analytics",
          "description": "Social media engagement metrics and audience analytics"
        }
      ]
    },
    "Email_Marketing": {
      "Templates": [
        {
          "name": "Email_Templates",
          "description": "Email campaign templates and designs for different purposes"
        }
      ],
      "Reports": [
        {
          "name": "Email_Performance",
          "description": "Email campaign analytics including open rates and click metrics"
        }
      ]
    },
    "Website": {
      "SOPs": [
        {
          "name": "Website_Updates",
          "description": "Website maintenance, content updates, and publishing procedures"
        }
      ],
      "Reports": [
        {
          "name": "Traffic_Analytics",
          "description": "Website traffic analysis, user behavior, and conversion tracking"
        }
      ]
    },
    "Partnerships": {
      "Agreements": [
        {
          "name": "Partnership_Contracts",
          "description": "Partner and affiliate agreement terms and conditions"
        }
      ],
      "Reports": [
        {
          "name": "Partner_Performance",
          "description": "Partner performance metrics and revenue sharing reports"
        }
      ]
    }
  },
  "Finance": {
    "Invoices": {
      "Vendor": [
        {
          "name": "Domestic_Vendor",
          "description": "Vendor invoices for domestic suppliers and service providers"
        }
      ],
      "Customer": [
        {
          "name": "Customer_Invoices",
          "description": "Customer invoices, billing statements, and payment terms"
        }
      ]
    },
    "Payments": {
      "SOPs": [
        {
          "name": "Payment_Approval",
          "description": "Payment authorization workflow and approval procedures"
        }
      ],
      "Reports": [
        {
          "name": "Payment_Status",
          "description": "Payment tracking and reconciliation reports"
        }
      ]
    },
    "Tax": {
      "Compliance": [
        {
          "name": "GST_Returns",
          "description": "GST/VAT filing and compliance returns"
        }
      ],
      "Reports": [
        {
          "name": "Tax_Audit",
          "description": "Tax audit findings and compliance documentation"
        }
      ]
    },
    "Audit": {
      "Reports": [
        {
          "name": "Internal_Audit",
          "description": "Internal audit reports and findings"
        },
        {
          "name": "External_Audit",
          "description": "External audit reports and management letters"
        }
      ]
    },
    "Budgeting": {
      "Plans": [
        {
          "name": "Annual_Budget",
          "description": "Annual budget planning and allocation by department"
        }
      ]
    },
    "Expenses": {
      "Policies": [
        {
          "name": "Travel_Expense",
          "description": "Travel expense policy, reimbursement limits, and approval process"
        }
      ]
    },
    "General_Ledger": {
      "SOPs": [
        {
          "name": "Journal_Entries",
          "description": "Journal entry posting procedures and accounting standards"
        }
      ],
      "Reports": [
        {
          "name": "Trial_Balance",
          "description": "General ledger trial balance and account reconciliation"
        }
      ]
    },
    "Accounts_Payable": {
      "SOPs": [
        {
          "name": "Invoice_Processing",
          "description": "Vendor invoice receipt, matching, and processing procedures"
        }
      ],
      "Reports": [
        {
          "name": "Aging_Report",
          "description": "Accounts payable aging analysis and vendor payment tracking"
        }
      ]
    },
    "Accounts_Receivable": {
      "SOPs": [
        {
          "name": "Invoice_Collection",
          "description": "Customer invoice collection and payment follow-up procedures"
        }
      ],
      "Reports": [
        {
          "name": "Receivables_Aging",
          "description": "Accounts receivable aging report and collection metrics"
        }
      ]
    },
    "Financial_Reporting": {
      "Reports": [
        {
          "name": "Monthly_Statements",
          "description": "Monthly income statement, balance sheet, and cash flow"
        },
        {
          "name": "Quarterly_Results",
          "description": "Quarterly financial statements and management discussion"
        }
      ]
    },
    "Cost_Management": {
      "SOPs": [
        {
          "name": "Cost_Allocation",
          "description": "Cost center allocation and overhead distribution procedures"
        }
      ],
      "Reports": [
        {
          "name": "Cost_Analysis",
          "description": "Cost variance analysis and cost control reporting"
        }
      ]
    },
    "Treasury": {
      "SOPs": [
        {
          "name": "Cash_Management",
          "description": "Cash management, liquidity planning, and working capital procedures"
        }
      ],
      "Reports": [
        {
          "name": "Cash_Flow",
          "description": "Cash flow forecasting and liquidity analysis"
        }
      ]
    },
    "Fixed_Assets": {
      "SOPs": [
        {
          "name": "Asset_Depreciation",
          "description": "Fixed asset depreciation calculation and policy"
        }
      ],
      "Reports": [
        {
          "name": "Asset_Schedule",
          "description": "Fixed asset register and depreciation schedule"
        }
      ]
    }
  },
  "HR": {
    "Policies": {
      "Leave": [
        {
          "name": "Annual_Leave",
          "description": "Annual leave entitlement, accrual, and usage policy"
        }
      ],
      "Attendance": [
        {
          "name": "Late_Coming",
          "description": "Attendance policy, tardiness procedures, and disciplinary action"
        }
      ],
      "Payroll": [
        {
          "name": "Salary_Process",
          "description": "Salary calculation, payment schedule, and payroll procedures"
        }
      ],
      "Code_of_Conduct": [
        {
          "name": "Employee_Behaviour",
          "description": "Code of conduct, expected behavior, and disciplinary policy"
        }
      ]
    },
    "Recruitment": {
      "SOPs": [
        {
          "name": "Interview_Process",
          "description": "Interview procedure, candidate evaluation, and selection process"
        }
      ],
      "Reports": [
        {
          "name": "Hiring_Status",
          "description": "Hiring pipeline metrics and recruitment dashboard reports"
        }
      ],
      "Templates": [
        {
          "name": "Job_Description",
          "description": "Job description templates for different role levels"
        }
      ],
      "Policies": [
        {
          "name": "Hiring_Policy",
          "description": "Hiring criteria, qualification requirements, and recruitment process"
        }
      ]
    },
    "Training": {
      "Manuals": [
        {
          "name": "New_Joiner_Training",
          "description": "New employee orientation and induction training program"
        }
      ],
      "Reports": [
        {
          "name": "Training_Attendance",
          "description": "Training attendance tracking and employee development records"
        }
      ]
    },
    "Employee_Handbook": {
      "Policies": [
        {
          "name": "General_HR_Rules",
          "description": "General HR policies, employee rights, and workplace rules"
        }
      ]
    },
    "Performance": {
      "Reviews": [
        {
          "name": "Annual_Appraisal",
          "description": "Annual performance appraisal process and rating system"
        }
      ]
    },
    "Onboarding": {
      "Manuals": [
        {
          "name": "Onboarding_Checklist",
          "description": "Onboarding checklist covering IT setup, paperwork, and orientation"
        }
      ],
      "SOPs": [
        {
          "name": "Induction_Process",
          "description": "Formal induction process and first-week activities"
        }
      ]
    },
    "Compensation": {
      "Policies": [
        {
          "name": "Compensation_Policy",
          "description": "Compensation structure, benchmarking, and pay equity policy"
        }
      ],
      "Reports": [
        {
          "name": "Salary_Review",
          "description": "Salary review recommendations and adjustment reports"
        }
      ]
    },
    "Benefits": {
      "Policies": [
        {
          "name": "Health_Insurance",
          "description": "Health insurance coverage, benefits, and enrollment process"
        },
        {
          "name": "Retirement_Plan",
          "description": "Retirement plan, pension scheme, and post-retirement benefits"
        }
      ],
      "SOPs": [
        {
          "name": "Benefits_Enrollment",
          "description": "Benefits enrollment procedures and open enrollment period"
        }
      ]
    },
    "Employee_Separation": {
      "SOPs": [
        {
          "name": "Exit_Process",
          "description": "Employee resignation, exit interview, and offboarding process"
        }
      ],
      "Checklists": [
        {
          "name": "Offboarding",
          "description": "Offboarding checklist including asset recovery and documentation"
        }
      ]
    },
    "Learning_Development": {
      "Plans": [
        {
          "name": "Training_Plan",
          "description": "Annual training and development plan by department"
        }
      ],
      "Reports": [
        {
          "name": "Skill_Assessment",
          "description": "Employee skill assessment and capability gap analysis"
        }
      ]
    },
    "Compliance": {
      "Policies": [
        {
          "name": "Labor_Laws",
          "description": "Labor law compliance documentation and required certifications"
        }
      ],
      "Reports": [
        {
          "name": "Compliance_Tracking",
          "description": "HR compliance audit and regulatory adherence tracking"
        }
      ]
    }
  },
  "Operations": {
    "Logistics": {
      "SOPs": [
        {
          "name": "Warehouse_Dispatch",
          "description": "Warehouse operations and dispatch procedures"
        }
      ],
      "Reports": [
        {
          "name": "Delivery_Performance",
          "description": "Delivery performance metrics and logistics KPIs"
        }
      ]
    },
    "Inventory": {
      "Reports": [
        {
          "name": "Stock_Reconciliation",
          "description": "Inventory reconciliation and stock variance analysis"
        }
      ],
      "SOPs": [
        {
          "name": "Stock_Adjustment",
          "description": "Stock adjustment procedures and inventory correction process"
        }
      ]
    },
    "Vendor_Management": {
      "Agreements": [
        {
          "name": "Vendor_Contracts",
          "description": "Vendor contracts and supplier agreement terms"
        }
      ]
    },
    "Process_Documents": {
      "SOPs": [
        {
          "name": "Daily_Operations",
          "description": "Daily operational procedures and routine activities"
        }
      ]
    },
    "Quality_Control": {
      "SOPs": [
        {
          "name": "QC_Procedures",
          "description": "Quality control inspection procedures and acceptance criteria"
        }
      ],
      "Reports": [
        {
          "name": "Quality_Metrics",
          "description": "Quality metrics and defect rate analysis reports"
        }
      ]
    },
    "Maintenance": {
      "SOPs": [
        {
          "name": "Preventive_Maintenance",
          "description": "Preventive maintenance schedules and equipment care procedures"
        }
      ],
      "Reports": [
        {
          "name": "Maintenance_Logs",
          "description": "Equipment maintenance logs and service history"
        }
      ]
    },
    "Supplier_Quality": {
      "SOPs": [
        {
          "name": "Supplier_Audit",
          "description": "Supplier quality audit procedures and assessment criteria"
        }
      ],
      "Reports": [
        {
          "name": "Quality_Rating",
          "description": "Supplier quality ratings and performance scorecards"
        }
      ]
    },
    "Supply_Chain": {
      "Plans": [
        {
          "name": "Supply_Plan",
          "description": "Supply chain planning and demand forecasting"
        }
      ],
      "Reports": [
        {
          "name": "Supply_Metrics",
          "description": "Supply chain performance metrics and efficiency analysis"
        }
      ]
    },
    "Production": {
      "SOPs": [
        {
          "name": "Production_Scheduling",
          "description": "Production scheduling and capacity planning procedures"
        }
      ],
      "Reports": [
        {
          "name": "Production_Output",
          "description": "Production output and efficiency reports"
        }
      ]
    }
  },
  "Legal": {
    "Contracts": {
      "Customer": [
        {
          "name": "Standard_Agreement",
          "description": "Standard customer service agreements and terms"
        }
      ],
      "Vendor": [
        {
          "name": "Vendor_Agreement",
          "description": "Vendor and supplier agreement terms and conditions"
        }
      ]
    },
    "Compliance": {
      "Policies": [
        {
          "name": "Data_Privacy",
          "description": "Data privacy policy and personal data protection procedures"
        }
      ],
      "Reports": [
        {
          "name": "Compliance_Audit",
          "description": "Legal compliance audit and assessment reports"
        }
      ]
    },
    "Notices": {
      "Customer": [
        {
          "name": "Legal_Notice",
          "description": "Legal notices and correspondence to customers"
        }
      ]
    },
    "Agreements": {
      "NDA": [
        {
          "name": "Non_Disclosure_Agreement",
          "description": "Non-disclosure agreements and confidentiality contracts"
        }
      ]
    },
    "Intellectual_Property": {
      "Policies": [
        {
          "name": "IP_Policy",
          "description": "Intellectual property ownership and protection policy"
        }
      ],
      "Agreements": [
        {
          "name": "IP_Assignments",
          "description": "IP assignment agreements and employee IP policies"
        }
      ]
    },
    "Litigation": {
      "Reports": [
        {
          "name": "Case_Status",
          "description": "Litigation status tracking and case updates"
        }
      ],
      "Documents": [
        {
          "name": "Legal_Briefs",
          "description": "Legal briefs and court documents"
        }
      ]
    },
    "Employment_Law": {
      "Policies": [
        {
          "name": "Employment_Contract",
          "description": "Employment contract templates and offer letters"
        }
      ],
      "SOPs": [
        {
          "name": "Dispute_Resolution",
          "description": "Employment dispute resolution and grievance procedures"
        }
      ]
    },
    "Regulatory_Compliance": {
      "Reports": [
        {
          "name": "Compliance_Status",
          "description": "Regulatory compliance status and audit reports"
        }
      ]
    }
  },
  "IT": {
    "Infrastructure": {
      "SOPs": [
        {
          "name": "Server_Maintenance",
          "description": "Server maintenance schedules and infrastructure management"
        }
      ],
      "Reports": [
        {
          "name": "System_Uptime",
          "description": "System uptime reports and availability metrics"
        }
      ]
    },
    "Security": {
      "Policies": [
        {
          "name": "Password_Policy",
          "description": "Password policy, authentication, and access control standards"
        }
      ],
      "Reports": [
        {
          "name": "Security_Incident",
          "description": "Security incident reports and breach investigation logs"
        }
      ]
    },
    "Applications": {
      "Manuals": [
        {
          "name": "User_Guide",
          "description": "Application user guides and system documentation"
        }
      ],
      "SOPs": [
        {
          "name": "Application_Deployment",
          "description": "Application deployment procedures and release management"
        }
      ]
    },
    "Data_Management": {
      "Policies": [
        {
          "name": "Data_Backup",
          "description": "Data backup and recovery policy"
        }
      ],
      "SOPs": [
        {
          "name": "Backup_Procedures",
          "description": "Backup and disaster recovery step-by-step procedures"
        }
      ]
    },
    "Disaster_Recovery": {
      "Plans": [
        {
          "name": "DR_Plan",
          "description": "Disaster recovery and business continuity plan"
        }
      ],
      "Reports": [
        {
          "name": "DR_Drills",
          "description": "Disaster recovery drill reports and testing results"
        }
      ]
    },
    "Network": {
      "SOPs": [
        {
          "name": "Network_Management",
          "description": "Network administration and management procedures"
        }
      ],
      "Reports": [
        {
          "name": "Network_Performance",
          "description": "Network performance monitoring and bandwidth analysis"
        }
      ]
    },
    "Database": {
      "SOPs": [
        {
          "name": "Database_Admin",
          "description": "Database administration and maintenance procedures"
        }
      ],
      "Reports": [
        {
          "name": "Database_Health",
          "description": "Database health checks and performance monitoring reports"
        }
      ]
    },
    "End_User_Support": {
      "Manuals": [
        {
          "name": "IT_Support_Guide",
          "description": "IT support procedures and help desk ticketing system"
        }
      ],
      "SOPs": [
        {
          "name": "Support_Escalation",
          "description": "IT support escalation procedures and SLA definitions"
        }
      ]
    },
    "Vendor_Management": {
      "Agreements": [
        {
          "name": "Software_Licenses",
          "description": "Software licensing agreements and vendor contracts"
        }
      ],
      "Reports": [
        {
          "name": "License_Inventory",
          "description": "Software license inventory and compliance tracking"
        }
      ]
    }
  },
  "Procurement": {
    "Purchase_Orders": {
      "SOPs": [
        {
          "name": "PO_Creation",
          "description": "Purchase order creation and approval workflow"
        }
      ],
      "Reports": [
        {
          "name": "PO_Status",
          "description": "Purchase order tracking and status reporting"
        }
      ]
    },
    "Vendors": {
      "Evaluation": [
        {
          "name": "Vendor_Rating",
          "description": "Vendor evaluation and performance rating criteria"
        }
      ]
    },
    "Contracts": {
      "Agreements": [
        {
          "name": "Supplier_Agreement",
          "description": "Supplier agreements and contract management"
        }
      ]
    },
    "Supplier_Management": {
      "SOPs": [
        {
          "name": "Supplier_Onboarding",
          "description": "Supplier registration, qualification, and onboarding"
        }
      ],
      "Reports": [
        {
          "name": "Supplier_Performance",
          "description": "Supplier performance evaluation and scorecards"
        }
      ]
    },
    "RFQ_Bidding": {
      "Templates": [
        {
          "name": "RFQ_Template",
          "description": "Request for quotation templates and bid documents"
        }
      ],
      "SOPs": [
        {
          "name": "Bidding_Process",
          "description": "Bidding process and competitive procurement procedures"
        }
      ]
    },
    "Category_Management": {
      "Plans": [
        {
          "name": "Category_Strategy",
          "description": "Procurement category strategy and optimization plans"
        }
      ],
      "Reports": [
        {
          "name": "Spend_Analysis",
          "description": "Category spend analysis and savings opportunities"
        }
      ]
    }
  },
  "Customer_Support": {
    "Complaints": {
      "SOPs": [
        {
          "name": "Complaint_Resolution",
          "description": "Complaint resolution process and escalation procedures"
        }
      ],
      "Reports": [
        {
          "name": "Complaint_Trends",
          "description": "Complaint trend analysis and root cause identification"
        }
      ]
    },
    "Tickets": {
      "SOPs": [
        {
          "name": "Ticket_Escalation",
          "description": "Support ticket escalation and priority assignment"
        }
      ]
    },
    "FAQs": {
      "Knowledge_Base": [
        {
          "name": "Common_Questions",
          "description": "Frequently asked questions and knowledge base articles"
        }
      ]
    },
    "SLA_Management": {
      "Policies": [
        {
          "name": "SLA_Policy",
          "description": "Service level agreements and customer support commitments"
        }
      ],
      "Reports": [
        {
          "name": "SLA_Performance",
          "description": "SLA compliance tracking and performance reporting"
        }
      ]
    },
    "Knowledge_Management": {
      "Manuals": [
        {
          "name": "Product_Info",
          "description": "Product information and customer guides"
        }
      ],
      "SOPs": [
        {
          "name": "Content_Updates",
          "description": "Knowledge base content management and updates"
        }
      ]
    },
    "Customer_Feedback": {
      "Reports": [
        {
          "name": "Feedback_Analysis",
          "description": "Customer feedback analysis and satisfaction surveys"
        }
      ],
      "SOPs": [
        {
          "name": "Feedback_Process",
          "description": "Customer feedback collection and response process"
        }
      ]
    }
  },
  "Compliance": {
    "Regulatory": {
      "Policies": [
        {
          "name": "Statutory_Compliance",
          "description": "Statutory compliance requirements and regulatory obligations"
        }
      ],
      "Reports": [
        {
          "name": "Regulatory_Filing",
          "description": "Regulatory filing and compliance reporting"
        }
      ]
    },
    "Risk": {
      "Reports": [
        {
          "name": "Risk_Assessment",
          "description": "Enterprise risk assessment and mitigation planning"
        }
      ]
    },
    "Audit": {
      "Reports": [
        {
          "name": "Compliance_Checklist",
          "description": "Compliance audit checklist and testing procedures"
        }
      ]
    },
    "Internal_Controls": {
      "Policies": [
        {
          "name": "Control_Framework",
          "description": "Internal control framework and COSO compliance"
        }
      ],
      "Reports": [
        {
          "name": "Control_Testing",
          "description": "Internal control testing and validation reports"
        }
      ]
    },
    "Ethics_Compliance": {
      "Policies": [
        {
          "name": "Code_of_Ethics",
          "description": "Code of ethics and conduct for all employees"
        }
      ],
      "Training": [
        {
          "name": "Ethics_Training",
          "description": "Mandatory ethics and compliance training programs"
        }
      ]
    },
    "Data_Protection": {
      "Policies": [
        {
          "name": "Data_Privacy_Policy",
          "description": "Data privacy and GDPR compliance documentation"
        }
      ],
      "SOPs": [
        {
          "name": "Data_Handling",
          "description": "Data handling, security, and breach notification procedures"
        }
      ]
    }
  },
  "Admin": {
    "Facilities": {
      "SOPs": [
        {
          "name": "Office_Maintenance",
          "description": "Office maintenance and facility management procedures"
        },
        {
          "name": "Safety_Procedures",
          "description": "Workplace safety procedures and emergency protocols"
        }
      ],
      "Reports": [
        {
          "name": "Asset_Inspection",
          "description": "Asset inspection reports and facility condition assessments"
        }
      ],
      "Policies": [
        {
          "name": "Facility_Policy",
          "description": "Facility management and usage policy"
        }
      ]
    },
    "Travel": {
      "Policies": [
        {
          "name": "Business_Travel",
          "description": "Business travel policy and approval procedures"
        }
      ]
    },
    "Assets": {
      "Reports": [
        {
          "name": "Asset_Register",
          "description": "Fixed asset register and equipment inventory"
        }
      ]
    },
    "Office_Services": {
      "SOPs": [
        {
          "name": "Procurement_Services",
          "description": "Office supplies procurement and ordering procedures"
        }
      ],
      "Reports": [
        {
          "name": "Supplies_Inventory",
          "description": "Office supplies inventory and consumption tracking"
        }
      ]
    },
    "Real_Estate": {
      "Contracts": [
        {
          "name": "Lease_Agreements",
          "description": "Property lease agreements and rental contracts"
        }
      ],
      "Reports": [
        {
          "name": "Property_Register",
          "description": "Real estate and property asset register"
        }
      ]
    }
  },
  "Management": {
    "Strategy": {
      "Plans": [
        {
          "name": "Annual_Strategy",
          "description": "Annual strategy and long-term business planning"
        }
      ],
      "Reports": [
        {
          "name": "Business_Review",
          "description": "Business review and strategic performance analysis"
        }
      ]
    },
    "Meetings": {
      "Minutes": [
        {
          "name": "Board_Meeting",
          "description": "Board meeting minutes and executive decisions"
        }
      ]
    },
    "KPIs": {
      "Reports": [
        {
          "name": "Monthly_KPI_Report",
          "description": "Monthly KPI reporting and performance dashboard"
        }
      ]
    },
    "Executive_Reports": {
      "Reports": [
        {
          "name": "Executive_Summary",
          "description": "Executive summaries and management reports"
        },
        {
          "name": "Board_Reports",
          "description": "Detailed board meeting reports and presentations"
        }
      ]
    },
    "Risk_Management": {
      "Policies": [
        {
          "name": "Risk_Policy",
          "description": "Enterprise risk management policy and framework"
        }
      ],
      "Reports": [
        {
          "name": "Risk_Register",
          "description": "Enterprise risk register and mitigation tracking"
        }
      ]
    },
    "Performance_Management": {
      "Plans": [
        {
          "name": "Company_Goals",
          "description": "Company goals and strategic objectives"
        }
      ],
      "Reports": [
        {
          "name": "Performance_Review",
          "description": "Company performance reviews and results analysis"
        }
      ]
    }
  }
} as const;
