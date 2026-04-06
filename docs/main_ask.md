# Deployment Engineer Project

## Project Overview

Netic develops AI agents that handle customer communications (calls, texts, and web chats) for enterprises in essential services industries. Many of our tenants receive consumer leads from third party lead aggregators like Google Ads, Facebook, Angi, and Thumbtack. As a deployment engineer, one of your responsibilities will be building and configuring integrations between tenants and their lead sources.

For this project, you will build a sample integration that can receive and process leads from Angi.

## Your Challenge

Design and build a connection to Angi API route that does the following:

- **Hosts an API route for Angi to send leads to**
  - Note: For purposes of testing the demo, you should host the route locally using an ngrok domain
- **Stores lead data in a database**
- **Automatically sends an email** to the address specified in the lead with an intro message to start the appointment booking process

Please refer to Angi API documentation here. Note that you do not need to speak to Angi to ask them to send leads to your system. We will test the system with manual requests that mimic data that Angi would send.

We recommend using TypeScript or Python.

## Key Requirements

### 1. System Architecture

- Design an Angi integration that will process leads into the Netic system and automatically send messages to the lead
- Ensure all data is stored for future usage and analytics
- Build a functioning prototype demonstrating core capabilities
- Map the payload to an existing tenant on our system (you can create dummy data to do this, but you should include a mapping mechanism)

### 2. Challenges to Address

You do not necessarily need to build a solution to all of these challenges, though you should implement and understand the implementation for these challenges where your time allows. For challenges you do not have time to build a functional solution to address, please think about how you'd design a solution and be ready to speak about that.

#### Maximize Conversion Efficiency

Our customers care deeply about "speed to lead" and maximizing the ROI on their lead spend at third party aggregators. What strategies should we employ to maximize lead conversion on opportunities we receive from Angi?

#### Analytics

How can we analyze the conversion rate of Angi leads to booked jobs? What data would we need to store and how would we use that data? What metrics would be most useful for our customers to track?

#### Duplicate Lead Handling

Angi and other third party lead aggregators sometimes send duplicate leads to our tenants when consumers request service more than once. When this happens, they offer rebates to their customers if the customers can demonstrate what duplicative leads they received. What could we build for our customers to help them maximize their rebates and simplify the rebate process?

#### Monitoring

In the past, Angi has changed the lead format and implemented other changes that broke the leads system without warning us. How could we monitor this system to ensure it is still functioning properly and alert us when there are potential issues?

## Submission Format

- You may use any tools available at your discretion. We encourage the use of LLM tools to help build, however you are expected to understand the details of the solution you are presenting and will be asked questions about how it works and what it can and cannot do.
- You will build and submit a functioning system capable of managing and responding to leads from Angi.
- You will present the system you have built at the end of the day.
- You will submit all code and other artifacts via a public GitHub repository for the team to review.

## Questions?

If you have clarifying questions about the assignment, please email Charles Ide.

## About Netic

Netic is the autonomous AI revenue engine for enterprises in essential services industries, including HVAC, plumbing, electric, automotive service, consumer health, and more. Combining real-time AI agents with autonomous marketing campaigns, Netic captures every lead and maximizes workforce utilization, providing 24/7 support that drives millions in revenue. Using both customer data and third-party signals, Netic identifies proactive ways for businesses to grow their revenue. Founded in 2024 by former Scale AI executive Melisa Tokmak–who rose from a small village in Turkey to a leader in Silicon Valley–the company is backed by our generation's best investors: Founders Fund, Greylock, Mike Volpi (Hanabi Capital), Day One Ventures, and angels Alex Wang, Elad Gil, and Dylan Field. Headquartered in San Francisco, Netic is on a mission to bring frontier technologies to the industries that keep America running.
